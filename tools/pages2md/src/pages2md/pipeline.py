from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image
from tqdm.auto import tqdm

from .adapters import open_document
from .assets import AssetStore
from .chapters import chapters_from_map, detect_chapters
from .compare import compare_text
from .constants import AUTO_SPLIT_BYTES, DEFAULT_DPI, SCHEMA_VERSION, commit_version
from .formatting import format_and_lint
from .lists import normalize_lists
from .markdown import (
    merge_html_tables,
    normalize_heading_case,
    normalize_table_blocks,
    strict_page_markdown,
    title_case_heading,
    write_markdown,
)
from .model import Block, OcrObservation, PageResult
from .native import observation_dict, parse_native_observation, reconcile_observations
from .ocr import MlxUnlimitedOcr, OcrBackend, confidence_summary, split_multi_page_output
from .quality import adjacent_overlap, output_quality_warnings
from .util import atomic_json, sha256_file, slugify
from .util import atomic_text
from .verify import verify_bundle

FIGURE_KINDS = {"figure", "image", "diagram", "chart", "graphic", "illustration", "photo", "map"}
FORMULA_KINDS = {"formula", "equation", "display_formula"}


def convert(
    source: Path,
    output: Path,
    **options: Any,
) -> Path:
    """Convert a document in a private workspace and publish only final artifacts."""
    source = source.resolve()
    slug = slugify(source.stem if source.is_file() else source.name, "document")
    source_hash = _source_hash(source)
    version = commit_version()
    if not re.fullmatch(r"[0-9a-f]{40,64}", version):
        raise RuntimeError("pages2md source commit is unavailable")
    with tempfile.TemporaryDirectory(prefix="pages2md-") as temporary:
        workspace = _convert_workspace(source, Path(temporary), **options)
        return _publish_output(workspace, output.resolve(), slug, source_hash, version)


def _convert_workspace(
    source: Path,
    output: Path,
    *,
    dpi: int = DEFAULT_DPI,
    pages: str | None = None,
    split_mode: str = "auto",
    quality: str = "thorough",
    chapter_map: Path | None = None,
    languages: list[str] | None = None,
    resume: bool = True,
    multi_page: bool = True,
    force: bool = False,
    backend: OcrBackend | None = None,
) -> Path:
    if quality not in {"fast", "balanced", "thorough"}:
        raise ValueError(f"unsupported OCR quality: {quality}")
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    bundle = output.resolve() / slugify(source.stem if source.is_file() else source.name, "document")
    if force and bundle.exists():
        shutil.rmtree(bundle)
    backend = backend or MlxUnlimitedOcr()
    ocr_fingerprint = {
        "source_sha256": _source_hash(source),
        "backend": dict(backend.identity),
        "dpi": dpi,
        "pages": pages,
        "languages": languages or [],
        "multi_page": multi_page,
        "quality": quality,
        "code": _code_fingerprint(
            "adapters.py", "assets.py", "compare.py", "model.py", "native.py", "ocr.py", "pipeline.py", "quality.py"
        ),
    }
    assembly_fingerprint = {
        "split_mode": split_mode,
        "chapter_map_sha256": sha256_file(chapter_map) if chapter_map else None,
        "code": _code_fingerprint(
            "chapters.py", "formatting.py", "lists.py", "markdown.py", "model.py", "pipeline.py", "quality.py", "verify.py"
        ),
    }
    previous = _read_json(bundle / "metadata.json")
    can_resume = bool(resume and previous and previous.get("ocr_fingerprint") == ocr_fingerprint)
    same_assembly = bool(previous and previous.get("assembly_fingerprint") == assembly_fingerprint)
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "pages").mkdir(exist_ok=True)
    (bundle / "raw").mkdir(exist_ok=True)
    work = bundle / ".work"
    work.mkdir(exist_ok=True)
    if not can_resume and (bundle / "assets").exists():
        shutil.rmtree(bundle / "assets")
    assets = AssetStore(bundle / "assets", load_existing=can_resume)
    fingerprint = {"ocr": ocr_fingerprint, "assembly": assembly_fingerprint}
    started = time.time()
    document = open_document(source, work, assets, dpi=dpi, page_spec=pages)

    if document.kind == "epub":
        result = _write_epub(
            bundle,
            source,
            document,
            assets,
            fingerprint,
            ocr_fingerprint,
            assembly_fingerprint,
            started,
            split_mode,
        )
        shutil.rmtree(work, ignore_errors=True)
        return result

    page_results: list[PageResult] = []
    failed: list[dict[str, Any]] = []
    ocr_pages = []
    with tqdm(
        total=len(document.pages),
        desc=source.name,
        unit="page",
        dynamic_ncols=True,
        smoothing=0.1,
        disable=None,
    ) as progress:
        progress.set_postfix_str("checking pages", refresh=False)
        for page in document.pages:
            blank, ink_fraction = _is_visually_blank(page.image_path)
            if not blank:
                ocr_pages.append(page)
                continue
            result = _blank_page_result(page, ink_fraction)
            atomic_json(bundle / "pages" / f"page-{page.number:04d}.json", result.to_dict())
            page_results.append(result)
            progress.update()

        for group in _ocr_groups(ocr_pages, document.outline, multi_page=multi_page):
            page_paths = [bundle / "pages" / f"page-{page.number:04d}.json" for page in group]
            page_range = str(group[0].number)
            if len(group) > 1:
                page_range += f"-{group[-1].number}"
            progress.set_postfix_str(f"processing {page_range}")
            if can_resume and all(path.exists() for path in page_paths):
                page_results.extend(_page_from_dict(_read_json(path)) for path in page_paths)
                progress.update(len(group))
                continue
            try:
                group_observation, recognized = _recognize_primary(group, backend, bundle)
            except Exception as error:
                failed.extend({"page": page.number, "error": str(error)} for page in group)
                progress.update(len(group))
                continue
            aligned = _align_multi_results(group, recognized)
            for source_page, (raw, generation), page_path in zip(group, aligned, page_paths):
                progress.set_postfix_str(f"processing {source_page.number}")
                try:
                    generation = dict(generation)
                    if len(group) > 1:
                        generation["group_pages"] = [page.number for page in group]
                    primary = parse_native_observation(
                        raw,
                        mode="multi_base",
                        source_pages=list(generation.get("source_pages", [source_page.number])),
                        generation=generation,
                    )
                    # Canonical spans originate from the immutable group invocation;
                    # page segmentation is a deterministic parser view of that raw file.
                    primary.id = group_observation.id
                    candidates, candidate_warnings = _collect_page_candidates(
                        source_page,
                        primary,
                        group_observation,
                        backend,
                        bundle,
                        quality=quality,
                    )
                    blocks, recovery, validation_warnings = reconcile_observations(
                        primary,
                        candidates,
                        embedded_text=source_page.embedded.text,
                    )
                    validation_warnings.extend(candidate_warnings)
                    result = _page_result(
                        source_page,
                        primary,
                        group_observation,
                        candidates,
                        blocks,
                        recovery,
                        validation_warnings,
                        assets,
                        document.outline,
                    )
                    atomic_json(page_path, result.to_dict())
                    page_results.append(result)
                except Exception as error:
                    failed.append({"page": source_page.number, "error": str(error)})
                finally:
                    progress.update()

    page_results.sort(key=lambda item: item.number)
    _normalize_document_blocks(page_results)
    _merge_continued_tables(page_results)
    for result in page_results:
        normalize_table_blocks(result.blocks)
    available_pages = {page.number for page in page_results}
    for result in page_results:
        # Re-render resumed page records too, so assembly-only changes are applied.
        _apply_links_to_blocks(result.blocks, result.embedded.links)
        result.visual_markdown = strict_page_markdown(result, document.outline)
        retained_warnings = [
            warning
            for warning in result.warnings
            if warning.startswith("visual_") or warning == "unresolved_internal_link"
        ]
        if result.generation.get("multi_page_recovery"):
            retained_warnings.append("multi_page_recovered_corrupt_segment")
        result.comparison = compare_text(result.visual_markdown, result.embedded.text)
        result.warnings = sorted(
            set(
                [
                    *result.comparison.warnings,
                    *retained_warnings,
                    *output_quality_warnings(result.visual_markdown),
                ]
            )
        )
        result.visual_markdown, unresolved = _sanitize_page_links(result.visual_markdown, available_pages)
        if unresolved:
            result.warnings.append("unresolved_internal_link")
            result.comparison.warnings.append("unresolved_internal_link")
        result.visual.setdefault("canonical", {})["blocks"] = [asdict(block) for block in result.blocks]
        result.visual["canonical"]["markdown"] = result.visual_markdown
        atomic_json(bundle / "pages" / f"page-{result.number:04d}.json", result.to_dict())
    overlap_pages: set[int] = set()
    for previous_page, current_page in zip(page_results, page_results[1:]):
        if current_page.number == previous_page.number + 1 and adjacent_overlap(
            previous_page.visual_markdown, current_page.visual_markdown
        ):
            previous_page.warnings = sorted(
                set([*previous_page.warnings, "visual_adjacent_page_overlap"])
            )
            current_page.warnings = sorted(
                set([*current_page.warnings, "visual_adjacent_page_overlap"])
            )
            overlap_pages.update((previous_page.number, current_page.number))
    for result in page_results:
        if result.number in overlap_pages:
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
    files = write_markdown(
        bundle,
        page_results,
        chapters,
        split=split,
        title=_title(document, source),
        outline=document.outline,
    )
    format_result = format_and_lint([bundle / path for path in files])
    if not format_result.idempotent:
        warnings.append("markdown_formatter_not_idempotent")
    if format_result.preservation_skips:
        warnings.append("markdown_formatter_skipped_unsafe_change")
    if format_result.lint_errors:
        warnings.append("markdown_lint_failed")
    output_fingerprints = {path: sha256_file(bundle / path) for path in files}
    resume_stable = not (
        can_resume
        and same_assembly
        and not previous.get("failed_pages")
        and previous.get("page_count") == previous.get("requested_page_count")
        and previous.get("output_fingerprints")
        and previous["output_fingerprints"] != output_fingerprints
    )
    if not resume_stable:
        warnings.append("resume_output_changed")
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
        "ocr_fingerprint": ocr_fingerprint,
        "assembly_fingerprint": assembly_fingerprint,
        "source": str(source),
        "source_kind": document.kind,
        "page_count": len(page_results),
        "requested_page_count": len(document.pages),
        "model": dict(backend.identity),
        "multi_page": multi_page,
        "quality": quality,
        "split": split,
        "markdown_files": files,
        "warnings": sorted(set(warnings)),
        "formatting": {
            "formatter": "mdformat==1.0.0 + mdformat-gfm==1.0.0",
            "linter": "pymarkdownlnt==0.9.38",
            "idempotent": format_result.idempotent,
            "lint_errors": format_result.lint_errors,
            "preservation_skips": format_result.preservation_skips,
        },
        "output_fingerprints": output_fingerprints,
        "resume_stable": resume_stable,
        "failed_pages": failed,
        "review_required_blocks": sum(
            bool(block.metadata.get("review_required"))
            for page in page_results
            for block in page.blocks
        ),
        "duration_seconds": round(time.time() - started, 3),
        "platform": platform.platform(),
    }
    atomic_json(bundle / "metadata.json", metadata)
    _write_log(bundle, metadata)
    shutil.rmtree(work, ignore_errors=True)
    if failed:
        raise RuntimeError(f"{len(failed)} page(s) failed")
    verification = verify_bundle(bundle)
    if not verification.ok:
        raise RuntimeError(f"bundle verification failed: {'; '.join(verification.errors)}")
    return bundle


def _publish_output(
    workspace: Path,
    output: Path,
    slug: str,
    source_hash: str,
    version: str,
) -> Path:
    metadata = _read_json(workspace / "metadata.json") or {}
    markdown_files = list(metadata.get("markdown_files", ["book.md"]))
    split = bool(metadata.get("split") or any(path.startswith("chapters/") for path in markdown_files))
    contents = {
        path: _public_markdown(
            (workspace / path).read_text(encoding="utf-8"),
            source_hash,
            version,
            split=split,
            index=path == "book.md",
        )
        for path in markdown_files
    }
    figures = _referenced_figures(contents.values())
    output.mkdir(parents=True, exist_ok=True)
    file_target = output / f"{slug}.md"
    directory_target = output / slug

    if not split and not figures:
        if directory_target.exists():
            shutil.rmtree(directory_target)
        atomic_text(file_target, contents["book.md"])
        return file_target

    if file_target.exists():
        file_target.unlink()
    with tempfile.TemporaryDirectory(prefix=f"pages2md-{slug}-") as temporary:
        staged = Path(temporary) / slug
        staged.mkdir()
        if split:
            atomic_text(staged / "index.md", contents["book.md"])
            for path, content in contents.items():
                if path == "book.md":
                    continue
                atomic_text(staged / Path(path).name, content)
        else:
            atomic_text(staged / f"{slug}.md", contents["book.md"])
        for relative in figures:
            source = workspace / "assets" / "figures" / relative
            if not source.is_file():
                raise RuntimeError(f"referenced figure is missing: {relative}")
            destination = staged / "figures" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if directory_target.exists():
            shutil.rmtree(directory_target)
        shutil.copytree(staged, directory_target)
    return directory_target


def _public_markdown(
    markdown: str,
    source_hash: str,
    version: str,
    *,
    split: bool,
    index: bool,
) -> str:
    if split and index:
        markdown = re.sub(r"\]\(chapters/([^/)]+)", r"](\1", markdown)
    markdown = markdown.replace("../assets/figures/", "figures/")
    markdown = markdown.replace("assets/figures/", "figures/")
    front_matter = (
        "---\n"
        f"source_sha256: {source_hash}\n"
        f"pages2md_version: {version}\n"
        "---\n\n"
    )
    return front_matter + markdown.lstrip()


def _referenced_figures(markdowns) -> set[str]:
    figures: set[str] = set()
    for markdown in markdowns:
        for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
            clean = target.partition("#")[0].partition("?")[0]
            if clean.startswith("figures/"):
                relative = clean.removeprefix("figures/")
                if relative and ".." not in Path(relative).parts:
                    figures.add(relative)
    return figures


def _page_result(
    source_page,
    primary: OcrObservation,
    group_observation: OcrObservation,
    recoveries: list[OcrObservation],
    blocks: list[Block],
    recovery: list[dict[str, Any]],
    validation_warnings: list[str],
    assets,
    outline,
) -> PageResult:
    visual_markdown = _blocks_markdown(blocks, "")
    comparison = compare_text(visual_markdown, source_page.embedded.text)
    _materialize_figures(
        blocks,
        source_page.image_path,
        source_page.number,
        assets,
        source_page.source_assets,
        include_unclaimed=not primary.generation.get("merged_into"),
    )
    raw_root = "raw"
    consensus_observations = sorted({
        entry["recovery_observation"]
        for entry in recovery
        if entry.get("action") in {"selected_ocr_consensus", "selected_targeted_detail"}
        and entry.get("recovery_observation")
    })
    result = PageResult(
        number=source_page.number,
        image=source_page.image_path.name,
        visual_markdown=visual_markdown,
        blocks=blocks,
        embedded=source_page.embedded,
        comparison=comparison,
        warnings=sorted(set([*comparison.warnings, *validation_warnings])),
        generation=primary.generation,
        source_assets=source_page.source_assets,
        raw_ocr=primary.raw,
        visual={
            "multi_page": observation_dict(
                group_observation,
                raw_path=f"{raw_root}/{group_observation.id}.txt",
            ),
            "candidates": [
                observation_dict(item, raw_path=f"{raw_root}/{item.id}.txt") for item in recoveries
            ],
            "canonical": {
                "blocks": [asdict(block) for block in blocks],
                "authoritative_observation": (
                    "multi_base_with_targeted_detail" if consensus_observations else group_observation.id
                ),
                "selected_observations": consensus_observations,
            },
        },
        recovery=recovery,
    )
    return result


def _is_visually_blank(image_path: Path, *, maximum_ink_fraction: float = 0.0005) -> tuple[bool, float]:
    """Conservatively identify empty raster pages before asking the VLM to decode them."""
    with Image.open(image_path) as source:
        grayscale = source.convert("L")
        grayscale.thumbnail((256, 256), Image.Resampling.LANCZOS)
        histogram = grayscale.histogram()
    total = sum(histogram)
    ink_fraction = sum(histogram[:245]) / total if total else 0.0
    return ink_fraction <= maximum_ink_fraction, ink_fraction


def _blank_page_result(source_page, ink_fraction: float) -> PageResult:
    comparison = compare_text("", source_page.embedded.text)
    warning = "visual_blank_page"
    return PageResult(
        number=source_page.number,
        image=source_page.image_path.name,
        visual_markdown="",
        blocks=[],
        embedded=source_page.embedded,
        comparison=comparison,
        warnings=sorted(set([*comparison.warnings, warning])),
        generation={"mode": "blank", "ink_fraction": ink_fraction},
        source_assets=source_page.source_assets,
        visual={
            "canonical": {
                "blocks": [],
                "markdown": "",
                "authoritative_observation": "visual_blank_page_detector",
            }
        },
    )


def _recognize_primary(group, backend, bundle: Path):
    recognize_pages = getattr(backend, "recognize_pages", None)
    if callable(recognize_pages):
        value = recognize_pages([page.image_path for page in group])
        if _is_invocation(value):
            raw, generation = value
            parts = split_multi_page_output(raw, len(group))
            recognized = [
                (
                    part,
                    {
                        **dict(generation),
                        **_segment_confidence(raw, part, generation),
                        "group_index": index,
                    },
                )
                for index, part in enumerate(parts)
            ]
        else:
            recognized = list(value)
            raw = "\n<PAGE>\n".join(item[0] for item in recognized)
            generation = {"mode": "multi_base", "group_size": len(group), "compat_backend": True}
    else:
        # Fixture compatibility. The production backend always exposes the
        # documented multi-page Base contract, including for one-page windows.
        recognized = [backend.recognize(page.image_path) for page in group]
        raw = "\n<PAGE>\n".join(item[0] for item in recognized)
        generation = {"mode": "multi_base", "group_size": len(group), "compat_backend": True}
    observation = parse_native_observation(
        raw,
        mode="multi_base",
        source_pages=[page.number for page in group],
        generation=generation,
    )
    if _segments_appear_reordered(group, recognized):
        observation.warnings = sorted(set([*observation.warnings, "visual_page_order_suspicious"]))
    _store_raw_observation(bundle, observation)
    return observation, recognized


def _segments_appear_reordered(group, recognized) -> bool:
    if len(group) != len(recognized) or len(group) < 2:
        return False
    from difflib import SequenceMatcher
    from .compare import normalize

    embedded = [normalize(page.embedded.text) for page in group]
    if sum(bool(value) for value in embedded) < 2:
        return False
    matches = []
    for raw, generation in recognized:
        observation = parse_native_observation(
            raw,
            mode="multi_base",
            source_pages=[page.number for page in group],
            generation=generation,
        )
        visual = normalize(_blocks_markdown(observation.blocks, ""))
        scores = [SequenceMatcher(None, visual, evidence, autojunk=False).ratio() for evidence in embedded]
        matches.append(max(range(len(scores)), key=scores.__getitem__))
    return matches != sorted(matches)


def _collect_page_candidates(
    source_page,
    primary,
    group_observation,
    backend,
    bundle,
    *,
    quality: str,
):
    critical = {
        "visual_empty_output",
        "visual_implausible_output_length",
        "visual_malformed_grounding",
        "visual_malformed_math",
        "visual_implausible_coordinates",
        "visual_text_repetition",
        "visual_truncated",
        "visual_malformed_table",
        "visual_page_transition_mismatch",
        "visual_page_order_suspicious",
    }
    markdown = _blocks_markdown(primary.blocks, "")
    comparison = compare_text(markdown, source_page.embedded.text)
    embedded_disagreement = (
        comparison.character_similarity is not None
        and comparison.character_similarity < 0.75
        and not (comparison.length_ratio is not None and comparison.length_ratio < 0.65)
    )
    group_problem = bool(set(group_observation.warnings) & critical)
    target_blocks = [
        (index, block)
        for index, block in enumerate(primary.blocks)
        if _block_needs_detail(block) and block.bbox is not None
    ]
    low_confidence = bool(target_blocks)
    structural_problem = bool(set(primary.warnings) & critical or group_problem)
    needs_recovery = bool(
        structural_problem
        or (quality in {"balanced", "thorough"} and embedded_disagreement)
        or (quality == "thorough" and low_confidence)
    )
    if quality == "fast" or (quality == "balanced" and not needs_recovery):
        return [], []
    if quality == "thorough" and not needs_recovery:
        return [], []

    candidates: list[OcrObservation] = []
    warnings: list[str] = []
    recognize_detail = getattr(backend, "recognize_detail", None)
    if not callable(recognize_detail):
        recognize_detail = getattr(backend, "recognize", None)
    if not callable(recognize_detail):
        return [], ["visual_auxiliary_ocr_unavailable"]
    try:
        raw, generation = recognize_detail(source_page.image_path)
        generation = {
            **dict(generation),
            "target_block_indices": [index for index, _ in target_blocks],
            "target_reason": (
                "structural_or_embedded_disagreement"
                if structural_problem or embedded_disagreement
                else "local_token_confidence"
            ),
        }
        candidate = parse_native_observation(
            raw,
            mode="gundam_detail",
            source_pages=[source_page.number],
            generation=generation,
        )
        _store_raw_observation(bundle, candidate)
        candidates.append(candidate)
    except Exception:
        warnings.append("visual_auxiliary_ocr_failed")

    # A structurally invalid multi-page segment needs an independent page-level
    # Base candidate as well as the cropped Gundam view. This is deliberately a
    # different visual contract, not another deterministic sample of the same
    # prompt.
    recognize_pages = getattr(backend, "recognize_pages", None)
    detail_failed = not candidates or bool(
        set(candidates[0].warnings)
        & {
            "visual_empty_output",
            "visual_implausible_output_length",
            "visual_malformed_grounding",
            "visual_malformed_math",
            "visual_malformed_table",
            "visual_text_repetition",
            "visual_truncated",
        }
    )
    if structural_problem and detail_failed and callable(recognize_pages):
        try:
            value = recognize_pages([source_page.image_path])
            if _is_invocation(value):
                raw, generation = value
                raw = split_multi_page_output(raw, 1)[0]
            else:
                raw, generation = list(value)[0]
            generation = {
                **dict(generation),
                "mode": "single_page_base",
                "target_reason": "structural_recovery",
                "source_pages": [source_page.number],
            }
            candidate = parse_native_observation(
                raw,
                mode="single_page_base",
                source_pages=[source_page.number],
                generation=generation,
            )
            _store_raw_observation(bundle, candidate)
            candidates.append(candidate)
        except Exception:
            warnings.append("visual_single_page_base_failed")
    return candidates, sorted(set(warnings))


def _block_needs_detail(block: Block) -> bool:
    return bool(block.metadata.get("uncertain_spans"))


def _segment_confidence(raw: str, part: str, generation: dict[str, object]) -> dict[str, object]:
    spans = generation.get("_confidence_spans", [])
    if not isinstance(spans, list) or not spans:
        return {"confidence": generation.get("confidence")}
    start = raw.find(part)
    if start < 0:
        return {"confidence": generation.get("confidence")}
    end = start + len(part)
    selected = []
    values: list[float] = []
    for span in spans:
        span_start = int(span.get("start", 0))
        span_end = int(span.get("end", 0))
        if span_end <= start or span_start >= end:
            continue
        logprobs = [float(value) for value in span.get("logprobs", [])]
        values.extend(logprobs)
        selected.append({
            "start": max(0, span_start - start),
            "end": min(len(part), span_end - start),
            "logprobs": logprobs,
        })
    return {"confidence": confidence_summary(values), "_confidence_spans": selected}


def _store_raw_observation(bundle: Path, observation: OcrObservation) -> Path:
    path = bundle / "raw" / f"{observation.id}.txt"
    if not path.exists():
        atomic_text(path, observation.raw)
    return path


def _is_invocation(value) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], dict)
    )


def _ocr_groups(pages, outline: list[dict], *, multi_page: bool, maximum: int = 8):
    if not multi_page:
        return [[page] for page in pages]
    by_number = {page.number: page for page in pages}
    numbers = sorted(by_number)
    if not numbers:
        return []
    starts = sorted(
        {
            item["page"]
            for item in outline
            if item.get("page") in by_number
            and (re.match(r"^\s*chapter\b", item.get("title", ""), re.I) or item.get("level") == 1)
        }
    )
    boundaries = starts or [numbers[0]]
    if boundaries[0] != numbers[0]:
        boundaries.insert(0, numbers[0])
    groups = []
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] - 1 if index + 1 < len(boundaries) else numbers[-1]
        section = [by_number[number] for number in numbers if start <= number <= end]
        runs = []
        for page in section:
            if not runs or page.number != runs[-1][-1].number + 1:
                runs.append([])
            runs[-1].append(page)
        for run in runs:
            groups.extend(run[offset : offset + maximum] for offset in range(0, len(run), maximum))
    covered = {page.number for group in groups for page in group}
    groups.extend([[by_number[number]] for number in numbers if number not in covered])
    return sorted(groups, key=lambda group: group[0].number)


def _align_multi_results(group, recognized):
    """Align fewer logical OCR segments to consecutive physical page ranges."""
    if len(recognized) == len(group):
        return [
            (raw, {**dict(generation), "source_pages": [page.number]})
            for page, (raw, generation) in zip(group, recognized)
        ]
    if not recognized:
        raise RuntimeError(
            f"cannot align {len(recognized)} OCR segment(s) to {len(group)} physical page(s)"
        )

    from difflib import SequenceMatcher
    from .compare import normalize

    segment_texts = [
        normalize(
            _blocks_markdown(
                parse_native_observation(
                    raw,
                    mode="multi_base",
                    source_pages=[page.number for page in group],
                    generation=generation,
                ).blocks,
                "",
            )
        )
        for raw, generation in recognized
    ]
    embedded = [normalize(page.embedded.text) for page in group]
    segment_count, page_count = len(recognized), len(group)
    if segment_count > page_count:
        scores: dict[tuple[int, int], tuple[float, list[tuple[int, int]]]] = {(0, 0): (0.0, [])}
        expected_span = segment_count / page_count
        for page_index in range(page_count):
            for segment_start in range(segment_count):
                state = scores.get((page_index, segment_start))
                if state is None:
                    continue
                remaining_pages = page_count - page_index - 1
                for segment_end in range(segment_start + 1, segment_count - remaining_pages + 1):
                    candidate_text = " ".join(segment_texts[segment_start:segment_end])
                    evidence = embedded[page_index]
                    similarity = (
                        SequenceMatcher(None, candidate_text, evidence, autojunk=False).ratio()
                        if evidence
                        else 0.0
                    )
                    span_penalty = 0.01 * abs((segment_end - segment_start) - expected_span)
                    candidate = state[0] + similarity - span_penalty
                    key = (page_index + 1, segment_end)
                    if key not in scores or candidate > scores[key][0]:
                        scores[key] = (candidate, [*state[1], (segment_start, segment_end)])
        alignment = scores.get((page_count, segment_count))
        if alignment is None:
            raise RuntimeError("over-segmented multi-page OCR could not be aligned monotonically")
        output = []
        for page, (start, end) in zip(group, alignment[1]):
            raw = "\n".join(item[0] for item in recognized[start:end])
            generation = dict(recognized[start][1])
            generation.update({
                "source_pages": [page.number],
                "merged_output_segments": end - start,
                "group_size": len(group),
            })
            output.append((raw, generation))
        return output

    scores: dict[tuple[int, int], tuple[float, list[tuple[int, int]]]] = {(0, 0): (0.0, [])}
    for segment_index in range(segment_count):
        for page_start in range(page_count):
            state = scores.get((segment_index, page_start))
            if state is None:
                continue
            remaining_segments = segment_count - segment_index - 1
            for page_end in range(page_start + 1, page_count - remaining_segments + 1):
                evidence = " ".join(embedded[page_start:page_end]).strip()
                similarity = (
                    SequenceMatcher(None, segment_texts[segment_index], evidence, autojunk=False).ratio()
                    if evidence
                    else 0.0
                )
                span_penalty = 0.01 * (page_end - page_start - 1)
                candidate = state[0] + similarity - span_penalty
                key = (segment_index + 1, page_end)
                if key not in scores or candidate > scores[key][0]:
                    scores[key] = (candidate, [*state[1], (page_start, page_end)])
    alignment = scores.get((segment_count, page_count))
    if alignment is None:
        raise RuntimeError("multi-page OCR segments could not be aligned monotonically")

    output: list[tuple[str, dict[str, object]]] = []
    for (raw, generation), (start, end) in zip(recognized, alignment[1]):
        source_pages = [page.number for page in group[start:end]]
        output.append((raw, {**dict(generation), "source_pages": source_pages}))
        for continuation in group[start + 1 : end]:
            output.append(
                (
                    "",
                    {
                        "mode": "multi_continuation",
                        "merged_into": group[start].number,
                        "source_pages": source_pages,
                        "group_size": len(group),
                    },
                )
            )
    return output


def _merge_continued_tables(pages: list[PageResult]) -> None:
    active: Block | None = None
    active_page: int | None = None
    for page in pages:
        retained: list[Block] = []
        suppressed_embedded = False
        if (
            active is not None
            and not page.blocks
            and re.fullmatch(r"(?:!\[[^\]]*\]\([^)]+\)\s*)+", page.visual_markdown.strip())
        ):
            # Resume compatibility for records written after an earlier pass
            # removed the duplicate block but retained its rendered fallback.
            page.visual_markdown = ""
        for block in page.blocks:
            # A logical multi-page result may store a continued table on its
            # first physical page. Unclaimed PDF objects before the next prose
            # block are then table slices already represented semantically.
            # Preserve them in the manifest, but do not display them twice.
            if block.kind == "embedded_figure" and (
                page.generation.get("merged_into") or active is not None
            ):
                suppressed_embedded = True
                continue
            if block.kind == "table":
                if active is not None:
                    boundary_geometry = bool(
                        active.bbox
                        and block.bbox
                        and active.bbox[3] >= 800
                        and block.bbox[1] <= 250
                    )
                    merged = merge_html_tables(
                        active.markdown,
                        block.markdown,
                        adjacent=active_page is not None and page.number == active_page + 1,
                        boundary_geometry=boundary_geometry,
                    )
                    if merged is not None:
                        active.markdown = merged
                        active.source_pages = sorted(set([*active.source_pages, *block.source_pages, page.number]))
                        active.provenance.extend(block.provenance)
                        active.metadata["multi_page_table"] = True
                        active_page = page.number
                        continue
                active = block
                active_page = page.number
                retained.append(block)
            else:
                retained.append(block)
                if block.kind not in FIGURE_KINDS | {"embedded_figure", "footer"} and block.markdown.strip():
                    active = None
                    active_page = None
        page.blocks = retained
        if suppressed_embedded and not retained:
            page.visual_markdown = ""


def _normalize_document_blocks(pages: list[PageResult]) -> None:
    """Apply deterministic structural cleanup to normalized blocks, never raw OCR."""
    from collections import Counter
    from .compare import normalize

    repeated = Counter()
    repeated_top = Counter()
    for page in pages:
        for index, block in enumerate(page.blocks):
            if not block.markdown.strip() or not block.bbox:
                if index < 2:
                    key = normalize(block.markdown)
                    if key and len(key) <= 120:
                        repeated_top[key] += 1
                continue
            if block.kind in {"header", "footer"} or block.bbox[1] <= 80 or block.bbox[3] >= 940:
                key = normalize(block.markdown)
                if key:
                    repeated[key] += 1
    boilerplate = {key for key, count in repeated.items() if count >= 2}
    ungrounded_boilerplate = {key for key, count in repeated_top.items() if count >= 2}
    for page in pages:
        for block in page.blocks:
            if not block.source_pages:
                block.source_pages = [page.number]
        retained = []
        for index, block in enumerate(page.blocks):
            normalized = normalize(block.markdown)
            running_matter = bool(
                block.kind in {"page_number", "header", "footer"}
                or (
                    block.bbox
                    and normalized in boilerplate
                    and (block.bbox[1] <= 80 or block.bbox[3] >= 940)
                )
                or (
                    index < 2
                    and not block.bbox
                    and (normalized in ungrounded_boilerplate or bool(re.fullmatch(r"[ivxlcdm]+|\d+", normalized, re.I)))
                )
            )
            if not running_matter:
                retained.append(block)
        page.blocks = retained

    normalize_lists(pages)
    _trim_adjacent_duplicate_blocks(pages)

    for previous, current in zip(pages, pages[1:]):
        if current.number != previous.number + 1 or not previous.blocks or not current.blocks:
            continue
        left, right = previous.blocks[-1], current.blocks[0]
        if (
            left.kind == right.kind == "paragraph"
            and left.markdown.rstrip()
            and right.markdown.lstrip()
            and left.markdown.rstrip()[-1] not in ".!?;:"
            and right.markdown.lstrip()[0].islower()
        ):
            left.markdown = f"{left.markdown.rstrip()} {right.markdown.lstrip()}"
            left.source_pages = sorted(set([*left.source_pages, *right.source_pages, previous.number, current.number]))
            left.provenance.extend(right.provenance)
            left.metadata["cross_page_paragraph"] = True
            current.blocks.pop(0)

    for page in pages:
        for block in page.blocks:
            if block.kind == "paragraph":
                block.markdown = _clean_prose(block.markdown)

        retained: list[Block] = []
        index = 0
        while index < len(page.blocks):
            block = page.blocks[index]
            if block.kind in FIGURE_KINDS | {"embedded_figure"} and index + 1 < len(page.blocks):
                caption = page.blocks[index + 1]
                caption_text = caption.markdown.strip()
                close = bool(
                    block.bbox
                    and caption.bbox
                    and 0 <= caption.bbox[1] - block.bbox[3] <= 120
                )
                if caption.kind == "caption" and close:
                    block.markdown = f"{block.markdown.rstrip()}\n\n*{caption_text}*"
                    block.metadata["caption"] = caption_text
                    block.provenance.extend(caption.provenance)
                    retained.append(block)
                    index += 2
                    continue
            retained.append(block)
            index += 1
        page.blocks = retained


def _trim_adjacent_duplicate_blocks(pages: list[PageResult]) -> None:
    """Keep duplicated OCR content on the physical page supported by evidence."""
    from difflib import SequenceMatcher
    from .compare import normalize

    for previous, current in zip(pages, pages[1:]):
        if current.number != previous.number + 1 or not previous.blocks or not current.blocks:
            continue
        left, right = previous.blocks[-1], current.blocks[0]
        left_text, right_text = left.markdown.strip(), right.markdown.strip()
        left_norm, right_norm = normalize(left_text), normalize(right_text)
        if min(len(left_norm), len(right_norm)) < 120:
            continue
        exact = left_text.rfind(right_text)
        if exact >= 0 and exact >= len(left_text) * 0.25:
            left.markdown = left_text[:exact].rstrip()
            left.metadata["trimmed_adjacent_page_overlap"] = current.number
            previous.warnings.append("visual_adjacent_page_overlap_repaired")
            continue
        longest = SequenceMatcher(None, left_text, right_text, autojunk=False).find_longest_match()
        if (
            longest.size >= 0.75 * min(len(left_text), len(right_text))
            and longest.a + longest.size >= len(left_text) - 80
            and longest.b <= 80
        ):
            left.markdown = left_text[: longest.a].rstrip()
            left.metadata["trimmed_adjacent_page_overlap"] = current.number
            previous.warnings.append("visual_adjacent_page_overlap_repaired")
            continue
        similarity = SequenceMatcher(None, left_norm, right_norm, autojunk=False).ratio()
        if similarity < 0.92:
            continue
        left_support = SequenceMatcher(
            None, left_norm, normalize(previous.embedded.text), autojunk=False
        ).ratio()
        right_support = SequenceMatcher(
            None, right_norm, normalize(current.embedded.text), autojunk=False
        ).ratio()
        if right_support > left_support + 0.05:
            previous.blocks.pop()
            previous.warnings.append("visual_adjacent_page_overlap_repaired")
        elif left_support > right_support + 0.05:
            current.blocks.pop(0)
            current.warnings.append("visual_adjacent_page_overlap_repaired")


def _clean_prose(value: str) -> str:
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in value.splitlines()]
    value = "\n".join(lines).strip()
    value = re.sub(r"\\\)\s+([,.;:!?])", lambda match: r"\)" + match.group(1), value)
    return value


def _materialize_figures(
    blocks: list[Block],
    page_image: Path,
    page: int,
    assets: AssetStore,
    source_assets: list[dict[str, Any]],
    *,
    include_unclaimed: bool = True,
) -> None:
    claimed_assets: set[str] = set()
    for block in blocks:
        if block.kind != "table" or not block.bbox:
            continue
        for placement in source_assets:
            candidate = tuple(placement.get("bbox", ()))
            if len(candidate) == 4 and _iou(block.bbox, candidate) >= 0.25:
                claimed_assets.add(placement.get("asset_id", ""))
    for block in blocks:
        if block.bbox and block.kind in FIGURE_KINDS:
            caption = block.markdown.strip() or None
            alt = caption.splitlines()[0][:160] if caption else block.kind.capitalize()
            asset = _matching_original(block.bbox, source_assets, assets)
            if asset is None:
                asset = assets.add_crop(page_image, block.bbox, page=page, caption=caption, alt_text=alt)
            assets.add_placement(
                asset.id,
                page=page,
                bbox=block.bbox,
                method="ocr_detected_figure",
                source_object=asset.source_object,
                caption=caption,
                alt_text=alt,
            )
            block.asset_id = asset.id
            claimed_assets.add(asset.id)
            block.markdown = f"![{alt}]({asset.path})"
            if caption:
                block.markdown += f"\n\n*{caption}*"
        elif block.bbox and block.kind in FORMULA_KINDS and not _looks_like_math(block.markdown):
            evidence = assets.add_crop(
                page_image, block.bbox, page=page, caption=block.markdown or None, alt_text="Equation evidence", evidence=True
            )
            block.metadata["equation_evidence_asset_id"] = evidence.id
    if not include_unclaimed:
        return
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
        markdown, _ = _replace_plain_text_once(markdown, label, link.target)
    return markdown


def _apply_links_to_blocks(blocks: list[Block], links) -> None:
    for link in links:
        label = " ".join(link.text.split())
        if not label or not link.target:
            continue
        if any(f"]({link.target})" in block.markdown for block in blocks):
            continue
        candidates = [
            block
            for block in blocks
            if block.kind not in {"table", "figure", "embedded_figure"}
            and _link_label_pattern(label).search(block.markdown)
        ]
        if link.bbox:
            candidates.sort(
                key=lambda block: _bbox_coverage(link.bbox, block.bbox) if block.bbox else 0.0,
                reverse=True,
            )
        for block in candidates:
            updated, count = _replace_plain_text_once(block.markdown, label, link.target)
            if count:
                block.markdown = updated
                block.metadata.setdefault("links", []).append(
                    {"target": link.target, "source": "embedded_link_geometry"}
                )
                break


_PROTECTED_MARKDOWN = re.compile(
    r"```.*?```|~~~.*?~~~|`[^`\n]*`|!\[[^\]]*\]\([^)]*\)|"
    r"\[[^\]]*\]\([^)]*\)|<[^>]*>|\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$",
    re.DOTALL,
)


def _replace_plain_text_once(markdown: str, label: str, target: str) -> tuple[str, int]:
    """Link one visible occurrence without entering existing Markdown constructs."""
    pattern = _link_label_pattern(label)
    cursor = 0
    for protected in _PROTECTED_MARKDOWN.finditer(markdown):
        match = pattern.search(markdown, cursor, protected.start())
        if match:
            replacement = f"[{match.group(0)}]({target})"
            return markdown[: match.start()] + replacement + markdown[match.end() :], 1
        cursor = protected.end()
    match = pattern.search(markdown, cursor)
    if not match:
        return markdown, 0
    replacement = f"[{match.group(0)}]({target})"
    return markdown[: match.start()] + replacement + markdown[match.end() :], 1


def _link_label_pattern(label: str) -> re.Pattern[str]:
    prefix = r"(?<!\w)" if label[:1].isalnum() else ""
    suffix = r"(?!\w)" if label[-1:].isalnum() else ""
    return re.compile(prefix + re.escape(label) + suffix, re.IGNORECASE)


def _bbox_coverage(subject, container) -> float:
    left, top = max(subject[0], container[0]), max(subject[1], container[1])
    right, bottom = min(subject[2], container[2]), min(subject[3], container[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area = max(1.0, (subject[2] - subject[0]) * (subject[3] - subject[1]))
    return intersection / area


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


def _should_retry(comparison) -> bool:
    if "visual_text_repetition" in comparison.warnings:
        return True
    if comparison.character_similarity is None:
        return False
    # A much longer visual stream commonly means a table or diagram is absent
    # from the embedded layer. That is evidence disagreement, not OCR failure.
    if comparison.length_ratio is not None and comparison.length_ratio < 0.65:
        return False
    return (
        comparison.character_similarity < 0.75
        or comparison.length_ratio is None
        or not 0.65 <= comparison.length_ratio <= 1.5
    )


def _comparison_quality(comparison) -> float:
    similarity = comparison.character_similarity or 0.0
    ratio = comparison.length_ratio or 0.0
    length_score = max(0.0, 1.0 - abs(1.0 - ratio))
    repetition_penalty = 1.0 if "visual_text_repetition" in comparison.warnings else 0.0
    return similarity + 0.2 * length_score - repetition_penalty


def _write_epub(
    bundle: Path,
    source: Path,
    document,
    assets: AssetStore,
    fingerprint,
    ocr_fingerprint,
    assembly_fingerprint,
    started: float,
    split_mode: str,
) -> Path:
    total_size = sum(len(item["markdown"].encode("utf-8")) for item in document.semantic_chapters)
    split = split_mode == "chapters" or (split_mode == "auto" and total_size > AUTO_SPLIT_BYTES and len(document.semantic_chapters) >= 2)
    files = ["book.md"]
    chapters = []
    from .util import atomic_text
    if split:
        chapters_dir = bundle / "chapters"
        chapters_dir.mkdir(exist_ok=True)
        index = [f"# {title_case_heading(_title(document, source))}", "", "## Contents", ""]
        filenames = [f"{number:03d}-{slugify(item['title'])}.md" for number, item in enumerate(document.semantic_chapters, 1)]
        source_to_file = {item["source"]: filenames[number] for number, item in enumerate(document.semantic_chapters)}
        for index_number, item in enumerate(document.semantic_chapters):
            filename = filenames[index_number]
            item_title = title_case_heading(item["title"])
            markdown = normalize_heading_case(item["markdown"]).replace("](assets/", "](../assets/")
            for source_name, target_file in source_to_file.items():
                markdown = markdown.replace(f"]({Path(source_name).name}", f"]({target_file}")
            atomic_text(chapters_dir / filename, f"# {item_title}\n\n{markdown.rstrip()}\n")
            index.append(f"- [{item_title}](chapters/{filename})")
            files.append(f"chapters/{filename}")
            chapters.append({"title": item["title"], "file": f"chapters/{filename}", "evidence": "epub_spine"})
        atomic_text(bundle / "book.md", "\n".join(index) + "\n")
    else:
        body = [f"# {title_case_heading(_title(document, source))}", ""]
        for item in document.semantic_chapters:
            body.extend([
                f"## {title_case_heading(item['title'])}",
                "",
                normalize_heading_case(item["markdown"]),
                "",
            ])
            chapters.append({"title": item["title"], "file": "book.md", "evidence": "epub_spine"})
        atomic_text(bundle / "book.md", "\n".join(body).rstrip() + "\n")
    format_result = format_and_lint([bundle / path for path in files])
    output_fingerprints = {path: sha256_file(bundle / path) for path in files}
    assets.write_manifest()
    atomic_json(bundle / "document.json", {"schema_version": SCHEMA_VERSION, "kind": "epub", "chapters": chapters})
    formatting_warnings = []
    if not format_result.idempotent:
        formatting_warnings.append("markdown_formatter_not_idempotent")
    if format_result.preservation_skips:
        formatting_warnings.append("markdown_formatter_skipped_unsafe_change")
    if format_result.lint_errors:
        formatting_warnings.append("markdown_lint_failed")
    atomic_json(bundle / "metadata.json", {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "ocr_fingerprint": ocr_fingerprint,
        "assembly_fingerprint": assembly_fingerprint,
        "source": str(source),
        "source_kind": "epub",
        "split": split,
        "markdown_files": files,
        "warnings": formatting_warnings,
        "failed_pages": [],
        "formatting": {
            "formatter": "mdformat==1.0.0 + mdformat-gfm==1.0.0",
            "linter": "pymarkdownlnt==0.9.38",
            "idempotent": format_result.idempotent,
            "lint_errors": format_result.lint_errors,
            "preservation_skips": format_result.preservation_skips,
        },
        "output_fingerprints": output_fingerprints,
        "resume_stable": True,
        "duration_seconds": round(time.time() - started, 3),
    })
    _write_log(bundle, _read_json(bundle / "metadata.json"))
    verification = verify_bundle(bundle)
    if not verification.ok:
        raise RuntimeError(f"bundle verification failed: {'; '.join(verification.errors)}")
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


def _code_fingerprint(*names: str) -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for name in sorted(names):
        path = root / name
        digest.update(name.encode())
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
        visual=value.get("visual", {}),
        recovery=value.get("recovery", []),
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
