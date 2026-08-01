from __future__ import annotations

import json
import platform
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image

from . import __version__
from .adapters import open_document
from .assets import AssetStore
from .chapters import chapters_from_map, detect_chapters
from .compare import compare_text
from .constants import AUTO_SPLIT_BYTES, DEFAULT_DPI, MODEL_ID, MODEL_REVISION, SCHEMA_VERSION
from .markdown import write_markdown
from .model import Block, PageResult
from .ocr import MlxUnlimitedOcr, OcrBackend, parse_output
from .util import atomic_json, sha256_file, slugify
from .util import atomic_text

FIGURE_KINDS = {"figure", "image", "diagram", "chart", "graphic", "illustration", "photo", "map"}
FORMULA_KINDS = {"formula", "equation", "display_formula"}


def convert(
    source: Path,
    output: Path,
    *,
    dpi: int = DEFAULT_DPI,
    pages: str | None = None,
    split_mode: str = "auto",
    chapter_map: Path | None = None,
    languages: list[str] | None = None,
    resume: bool = True,
    force: bool = False,
    backend: OcrBackend | None = None,
) -> Path:
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    bundle = output.resolve() / slugify(source.stem if source.is_file() else source.name, "document")
    if force and bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "pages").mkdir(exist_ok=True)
    work = bundle / ".work"
    work.mkdir(exist_ok=True)
    assets = AssetStore(bundle / "assets")
    fingerprint = {
        "source_sha256": _source_hash(source),
        "tool_version": __version__,
        "model": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "dpi": dpi,
        "pages": pages,
        "languages": languages or [],
    }
    previous = _read_json(bundle / "metadata.json")
    can_resume = resume and previous and previous.get("fingerprint") == fingerprint
    started = time.time()
    document = open_document(source, work, assets, dpi=dpi, page_spec=pages)

    if document.kind == "epub":
        result = _write_epub(bundle, source, document, assets, fingerprint, started, split_mode)
        shutil.rmtree(work, ignore_errors=True)
        return result

    backend = backend or MlxUnlimitedOcr()
    page_results: list[PageResult] = []
    failed: list[dict[str, Any]] = []
    for source_page in document.pages:
        page_path = bundle / "pages" / f"page-{source_page.number:04d}.json"
        if can_resume and page_path.exists():
            page_results.append(_page_from_dict(_read_json(page_path)))
            continue
        try:
            raw, generation = backend.recognize(source_page.image_path)
            visual_markdown, blocks = parse_output(raw)
            _materialize_figures(blocks, source_page.image_path, source_page.number, assets, source_page.source_assets)
            visual_markdown = _blocks_markdown(blocks, visual_markdown)
            visual_markdown = _apply_links(visual_markdown, source_page.embedded.links)
            comparison = compare_text(visual_markdown, source_page.embedded.text)
            result = PageResult(
                number=source_page.number,
                image=source_page.image_path.name,
                visual_markdown=visual_markdown,
                blocks=blocks,
                embedded=source_page.embedded,
                comparison=comparison,
                warnings=list(comparison.warnings),
                generation=generation,
                source_assets=source_page.source_assets,
                raw_ocr=raw,
            )
            atomic_json(page_path, result.to_dict())
            page_results.append(result)
        except Exception as error:
            failed.append({"page": source_page.number, "error": str(error)})

    page_results.sort(key=lambda item: item.number)
    available_pages = {page.number for page in page_results}
    for result in page_results:
        result.visual_markdown, unresolved = _sanitize_page_links(result.visual_markdown, available_pages)
        if unresolved:
            result.warnings.append("unresolved_internal_link")
            result.comparison.warnings.append("unresolved_internal_link")
            atomic_json(bundle / "pages" / f"page-{result.number:04d}.json", result.to_dict())
    chapters = chapters_from_map(chapter_map, [page.number for page in page_results]) if chapter_map else detect_chapters(document, page_results)
    combined_size = sum(len(page.visual_markdown.encode("utf-8")) for page in page_results)
    if split_mode == "single":
        split = False
    elif split_mode == "chapters":
        if len(chapters) < 2:
            raise RuntimeError("reliable chapter boundaries were not found; provide --chapter-map")
        split = True
    else:
        split = combined_size > AUTO_SPLIT_BYTES and len(chapters) >= 2
    warnings = sorted({warning for page in page_results for warning in page.warnings})
    if split_mode == "auto" and combined_size > AUTO_SPLIT_BYTES and len(chapters) < 2:
        warnings.append("chapter_boundaries_uncertain")
    files = write_markdown(bundle, page_results, chapters, split=split, title=_title(document, source))
    assets.write_manifest()
    document_json = {
        "schema_version": SCHEMA_VERSION,
        "source": str(source),
        "kind": document.kind,
        "metadata": document.metadata,
        "outline": document.outline,
        "chapters": [asdict(chapter) for chapter in chapters],
        "pages": [page.to_dict() for page in page_results],
    }
    atomic_json(bundle / "document.json", document_json)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "source": str(source),
        "source_kind": document.kind,
        "page_count": len(page_results),
        "requested_page_count": len(document.pages),
        "model": dict(backend.identity),
        "split": split,
        "markdown_files": files,
        "warnings": sorted(set(warnings)),
        "failed_pages": failed,
        "duration_seconds": round(time.time() - started, 3),
        "platform": platform.platform(),
    }
    atomic_json(bundle / "metadata.json", metadata)
    _write_log(bundle, metadata)
    shutil.rmtree(work, ignore_errors=True)
    if failed:
        raise RuntimeError(f"{len(failed)} page(s) failed; successful pages are resumable in {bundle}")
    return bundle


def _materialize_figures(
    blocks: list[Block], page_image: Path, page: int, assets: AssetStore, source_assets: list[dict[str, Any]]
) -> None:
    claimed_assets: set[str] = set()
    for block in blocks:
        if block.bbox and block.kind in FIGURE_KINDS:
            caption = block.markdown.strip() or None
            alt = caption.splitlines()[0][:160] if caption else block.kind.capitalize()
            asset = _matching_original(block.bbox, source_assets, assets)
            if asset is None:
                asset = assets.add_crop(page_image, block.bbox, page=page, caption=caption, alt_text=alt)
            else:
                asset.caption = caption
                asset.alt_text = alt
            block.asset_id = asset.id
            claimed_assets.add(asset.id)
            block.markdown = f"![{asset.alt_text}]({asset.path})"
            if caption:
                block.markdown += f"\n\n*{caption}*"
        elif block.bbox and block.kind in FORMULA_KINDS and not _looks_like_math(block.markdown):
            assets.add_crop(
                page_image, block.bbox, page=page, caption=block.markdown or None, alt_text="Equation evidence", evidence=True
            )
            block.markdown = f"{block.markdown}\n\n<!-- uncertain equation; evidence crop retained -->".strip()
    for placement in source_assets:
        bbox = tuple(placement.get("bbox", ()))
        asset = assets.get(placement.get("asset_id", ""))
        if asset is None or asset.id in claimed_assets or len(bbox) != 4:
            continue
        area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
        if area < 10_000:
            continue
        blocks.append(
            Block(
                kind="embedded_figure",
                markdown=f"![{asset.alt_text}]({asset.path})",
                bbox=bbox,
                asset_id=asset.id,
            )
        )


def _blocks_markdown(blocks: list[Block], fallback: str) -> str:
    rendered = "\n\n".join(block.markdown.strip() for block in blocks if block.markdown.strip())
    return rendered or fallback


def _looks_like_math(value: str) -> bool:
    return any(marker in value for marker in ("$", "\\(", "\\[", "\\begin{"))


def _matching_original(bbox, placements, assets: AssetStore):
    best = None
    best_overlap = 0.0
    for placement in placements:
        candidate = tuple(placement.get("bbox", ()))
        if len(candidate) != 4:
            continue
        overlap = _iou(bbox, candidate)
        if overlap > best_overlap:
            best_overlap = overlap
            best = assets.get(placement.get("asset_id", ""))
    return best if best_overlap >= 0.25 else None


def _iou(a, b) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = max(1.0, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection)
    return intersection / union


def _apply_links(markdown: str, links) -> str:
    for link in links:
        label = " ".join(link.text.split())
        if not label or not link.target or f"]({link.target})" in markdown:
            continue
        import re
        pattern = re.compile(re.escape(label), re.IGNORECASE)
        markdown, count = pattern.subn(lambda match: f"[{match.group(0)}]({link.target})", markdown, count=1)
    return markdown


def _sanitize_page_links(markdown: str, available_pages: set[int]) -> tuple[str, list[int]]:
    import re

    unresolved: list[int] = []
    pattern = re.compile(r"\[([^\]]+)\]\(#page-(\d+)\)")

    def replace(match):
        page_number = int(match.group(2))
        if page_number in available_pages:
            return match.group(0)
        unresolved.append(page_number)
        return match.group(1)

    return pattern.sub(replace, markdown), sorted(set(unresolved))


def _write_epub(bundle: Path, source: Path, document, assets: AssetStore, fingerprint, started: float, split_mode: str) -> Path:
    total_size = sum(len(item["markdown"].encode("utf-8")) for item in document.semantic_chapters)
    split = split_mode == "chapters" or (split_mode == "auto" and total_size > AUTO_SPLIT_BYTES and len(document.semantic_chapters) >= 2)
    files = ["book.md"]
    chapters = []
    from .util import atomic_text
    if split:
        chapters_dir = bundle / "chapters"
        chapters_dir.mkdir(exist_ok=True)
        index = [f"# {_title(document, source)}", "", "## Contents", ""]
        filenames = [f"{number:03d}-{slugify(item['title'])}.md" for number, item in enumerate(document.semantic_chapters, 1)]
        source_to_file = {item["source"]: filenames[number] for number, item in enumerate(document.semantic_chapters)}
        for index_number, item in enumerate(document.semantic_chapters):
            filename = filenames[index_number]
            markdown = item["markdown"].replace("](assets/", "](../assets/")
            for source_name, target_file in source_to_file.items():
                markdown = markdown.replace(f"]({Path(source_name).name}", f"]({target_file}")
            atomic_text(chapters_dir / filename, f"# {item['title']}\n\n{markdown.rstrip()}\n")
            index.append(f"- [{item['title']}](chapters/{filename})")
            files.append(f"chapters/{filename}")
            chapters.append({"title": item["title"], "file": f"chapters/{filename}", "evidence": "epub_spine"})
        atomic_text(bundle / "book.md", "\n".join(index) + "\n")
    else:
        body = [f"# {_title(document, source)}", ""]
        for item in document.semantic_chapters:
            body.extend([f"## {item['title']}", "", item["markdown"], ""])
            chapters.append({"title": item["title"], "file": "book.md", "evidence": "epub_spine"})
        atomic_text(bundle / "book.md", "\n".join(body).rstrip() + "\n")
    assets.write_manifest()
    atomic_json(bundle / "document.json", {"schema_version": SCHEMA_VERSION, "kind": "epub", "chapters": chapters})
    atomic_json(bundle / "metadata.json", {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "source": str(source),
        "source_kind": "epub",
        "split": split,
        "markdown_files": files,
        "warnings": [],
        "failed_pages": [],
        "duration_seconds": round(time.time() - started, 3),
    })
    _write_log(bundle, _read_json(bundle / "metadata.json"))
    return bundle


def _title(document, source: Path) -> str:
    return document.metadata.get("title") or source.stem.replace("-", " ").replace("_", " ").strip().title()


def _source_hash(source: Path) -> str:
    if source.is_file():
        return sha256_file(source)
    import hashlib
    digest = hashlib.sha256()
    for path in sorted((item for item in source.rglob("*") if item.is_file())):
        digest.update(str(path.relative_to(source)).encode())
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _read_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _page_from_dict(value: dict[str, Any]) -> PageResult:
    from .model import Comparison, EmbeddedEvidence, Link
    embedded_value = value["embedded"]
    embedded = EmbeddedEvidence(
        text=embedded_value.get("text", ""),
        blocks=embedded_value.get("blocks", []),
        links=[Link(**link) for link in embedded_value.get("links", [])],
        extractor=embedded_value.get("extractor"),
    )
    return PageResult(
        number=value["number"], image=value["image"], visual_markdown=value["visual_markdown"],
        blocks=[Block(**block) for block in value.get("blocks", [])], embedded=embedded,
        comparison=Comparison(**value.get("comparison", {})), warnings=value.get("warnings", []),
        generation=value.get("generation", {}),
        source_assets=value.get("source_assets", []),
        raw_ocr=value.get("raw_ocr", ""),
    )


def _write_log(bundle: Path, metadata: dict[str, Any]) -> None:
    lines = [
        f"source={metadata.get('source')}",
        f"kind={metadata.get('source_kind')}",
        f"pages={metadata.get('page_count', 0)}",
        f"duration_seconds={metadata.get('duration_seconds')}",
    ]
    lines.extend(f"warning={warning}" for warning in metadata.get("warnings", []))
    lines.extend(f"failed_page={item.get('page')} error={item.get('error')}" for item in metadata.get("failed_pages", []))
    atomic_text(bundle / "conversion.log", "\n".join(lines) + "\n")
