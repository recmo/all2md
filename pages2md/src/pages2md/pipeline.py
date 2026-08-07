from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image
from tqdm.auto import tqdm

from .adapters import open_document
from .assets import AssetStore
from .chapters import detect_chapters
from .compare import compare_text
from .constants import AUTO_SPLIT_BYTES, DEFAULT_DPI, SCHEMA_VERSION, commit_version
from .formatting import format_and_lint
from .lists import normalize_lists
from .markdown import (
    merge_html_tables,
    normalize_table_blocks,
    strict_page_markdown,
    write_markdown,
)
from .model import Block, OcrObservation, PageResult
from .native import observation_dict, parse_native_observation, reconcile_observations
from .ocr import MlxUnlimitedOcr, OcrBackend, confidence_summary, split_multi_page_output
from .quality import adjacent_overlap, output_quality_warnings, runaway_repetition_span
from .util import atomic_json, sha256_file, slugify
from .util import atomic_text
from .verify import verify_bundle

FIGURE_KINDS = {"figure", "image", "diagram", "chart", "graphic", "illustration", "photo", "map"}
FORMULA_KINDS = {"formula", "equation", "display_formula"}


def convert(
    source: Path,
    *,
    force: bool = False,
    backend: OcrBackend | None = None,
) -> Path:
    """Convert a document in a private workspace and publish only final artifacts."""
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    target = source.with_name(f"{source.name}.md")
    if target.exists() and not force:
        raise FileExistsError(f"output exists (use --force): {target}")
    source_hash = _source_hash(source)
    version = commit_version()
    if not re.fullmatch(r"[0-9a-f]{40,64}", version):
        raise RuntimeError("pages2md source commit is unavailable")
    with tempfile.TemporaryDirectory(prefix="pages2md-") as temporary:
        workspace = _convert_workspace(source, Path(temporary), backend=backend)
        verification = verify_bundle(workspace)
        for warning in verification.warnings:
            print(f"pages2md: warning: {warning}", file=sys.stderr)
        return _publish_output(workspace, target, source_hash, version)


def _convert_workspace(
    source: Path,
    output: Path,
    *,
    backend: OcrBackend | None = None,
) -> Path:
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    bundle = output.resolve() / slugify(source.name, "document")
    backend = backend or MlxUnlimitedOcr()
    ocr_fingerprint = {
        "source_sha256": _source_hash(source),
        "backend": dict(backend.identity),
        "dpi": DEFAULT_DPI,
        "multi_page": True,
        "quality": "thorough",
        "code": _code_fingerprint(
            "adapters.py", "assets.py", "compare.py", "model.py", "native.py", "ocr.py", "pipeline.py", "quality.py"
        ),
    }
    assembly_fingerprint = {
        "split_mode": "auto",
        "code": _code_fingerprint(
            "chapters.py", "formatting.py", "lists.py", "markdown.py", "model.py", "pipeline.py", "quality.py", "verify.py"
        ),
    }
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "pages").mkdir(exist_ok=True)
    (bundle / "raw").mkdir(exist_ok=True)
    work = bundle / ".work"
    work.mkdir(exist_ok=True)
    assets = AssetStore(bundle / "assets")
    fingerprint = {"ocr": ocr_fingerprint, "assembly": assembly_fingerprint}
    started = time.time()
    document = open_document(source, work, assets, dpi=DEFAULT_DPI)

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

        for group in _ocr_groups(ocr_pages, document.outline):
            page_paths = [bundle / "pages" / f"page-{page.number:04d}.json" for page in group]
            page_range = str(group[0].number)
            if len(group) > 1:
                page_range += f"-{group[-1].number}"
            progress.set_postfix_str(f"processing {page_range}")
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
    chapters = detect_chapters(document, page_results)
    combined_size = sum(len(page.visual_markdown.encode("utf-8")) for page in page_results)
    split = combined_size > AUTO_SPLIT_BYTES and len(chapters) >= 2
    warnings = sorted({warning for page in page_results for warning in page.warnings})
    if combined_size > AUTO_SPLIT_BYTES and len(chapters) < 2:
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
        "multi_page": True,
        "quality": "thorough",
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
    target: Path,
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
    if not split and not figures:
        if target.is_dir():
            shutil.rmtree(target)
        atomic_text(target, contents["book.md"])
        return target

    if target.is_file():
        target.unlink()
    with tempfile.TemporaryDirectory(prefix=f"pages2md-{target.stem}-", dir=target.parent) as temporary:
        staged = Path(temporary) / target.name
        staged.mkdir()
        if split:
            atomic_text(staged / "index.md", contents["book.md"])
            for path, content in contents.items():
                if path == "book.md":
                    continue
                atomic_text(staged / Path(path).name, content)
        else:
            atomic_text(staged / target.name, contents["book.md"])
        for relative in figures:
            source = workspace / "assets" / "figures" / relative
            if not source.is_file():
                raise RuntimeError(f"referenced figure is missing: {relative}")
            destination = staged / "figures" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(staged, target)
    return target


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
    validation_warnings.extend(_repair_runaway_repetition(blocks))
    validation_warnings.extend(
        _canonicalize_figure_blocks(
            blocks,
            source_page.image_path,
        )
    )
    visual_markdown = _blocks_markdown(blocks, "")
    comparison = compare_text(visual_markdown, source_page.embedded.text)
    _materialize_figures(
        blocks,
        source_page.image_path,
        source_page.number,
        assets,
        source_page.source_assets,
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
        or embedded_disagreement
        or low_confidence
    )
    if not needs_recovery:
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


def _ocr_groups(pages, outline: list[dict], maximum: int = 8):
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
        for block in page.blocks:
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
                if block.kind not in FIGURE_KINDS | {"footer"} and block.markdown.strip():
                    active = None
                    active_page = None
        page.blocks = retained


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
            if block.kind in FIGURE_KINDS and index + 1 < len(page.blocks):
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
) -> None:
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
            block.markdown = f"![{alt}]({asset.path})"
            if caption:
                block.markdown += f"\n\n*{caption}*"
        elif block.bbox and block.kind in FORMULA_KINDS and not _looks_like_math(block.markdown):
            evidence = assets.add_crop(
                page_image, block.bbox, page=page, caption=block.markdown or None, alt_text="Equation evidence", evidence=True
            )
            block.metadata["equation_evidence_asset_id"] = evidence.id


def _repair_runaway_repetition(blocks: list[Block]) -> list[str]:
    groups: list[list[int]] = []
    current: list[int] = []
    for index, block in enumerate(blocks):
        if block.kind in {"table", *FORMULA_KINDS} or not block.markdown.strip():
            if current:
                groups.append(current)
                current = []
            continue
        current.append(index)
    if current:
        groups.append(current)

    repaired = False
    for indices in reversed(groups):
        original_characters = sum(len(blocks[index].markdown) for index in indices)
        affected: set[int] = set()
        for _ in range(128):
            active = [index for index in indices if blocks[index].markdown]
            combined, segments = _joined_block_text(blocks, active)
            span = runaway_repetition_span(combined, minimum_phrase_tokens=2)
            if span is None:
                break
            start, end = span
            for index, segment_start, segment_end in segments:
                overlap_start = max(start, segment_start)
                overlap_end = min(end, segment_end)
                if overlap_start >= overlap_end:
                    continue
                block = blocks[index]
                local_start = overlap_start - segment_start
                local_end = overlap_end - segment_start
                block.markdown = (
                    block.markdown[:local_start] + block.markdown[local_end:]
                ).strip()
                affected.add(index)
            repaired = True
        if not affected:
            continue
        anchor = next(
            (index for index in indices if blocks[index].markdown),
            indices[0],
        )
        blocks[anchor].metadata.update({
            "review_required": True,
            "review_reason": "runaway_repetition_truncated",
            "original_characters": original_characters,
            "retained_characters": sum(
                len(blocks[index].markdown) for index in indices
            ),
        })
        for index in sorted(affected, reverse=True):
            if index != anchor and not blocks[index].markdown:
                del blocks[index]
    return ["visual_text_repetition_repaired"] if repaired else []


def _joined_block_text(
    blocks: list[Block],
    indices: list[int],
) -> tuple[str, list[tuple[int, int, int]]]:
    parts: list[str] = []
    segments: list[tuple[int, int, int]] = []
    cursor = 0
    for index in indices:
        if parts:
            parts.append("\n\n")
            cursor += 2
        text = blocks[index].markdown
        segments.append((index, cursor, cursor + len(text)))
        parts.append(text)
        cursor += len(text)
    return "".join(parts), segments


def _canonicalize_figure_blocks(
    blocks: list[Block],
    page_image: Path,
) -> list[str]:
    """Clamp figure boxes and remove only provably blank or duplicate crops."""
    candidates: list[tuple[int, Block]] = []
    warnings: list[str] = []
    rejected: set[int] = set()
    with Image.open(page_image) as source:
        grayscale = source.convert("L")
        for index, block in enumerate(blocks):
            if block.kind not in FIGURE_KINDS or not block.bbox:
                continue
            block.bbox = _padded_bbox(block.bbox)
            blank, touches_edge = _figure_crop_status(grayscale, block.bbox)
            if blank:
                warnings.append("visual_blank_figure_crop_rejected")
                if block.markdown.strip():
                    block.metadata.update({
                        "review_required": True,
                        "review_reason": "blank_figure_crop_caption_preserved",
                    })
                    block.kind = "paragraph"
                    block.bbox = None
                else:
                    rejected.add(index)
                continue
            if touches_edge:
                block.metadata.update({
                    "review_required": True,
                    "review_reason": "figure_crop_touches_edge",
                })
                warnings.append("visual_figure_crop_may_be_clipped")
            candidates.append((index, block))

    # Only near-identical boxes are duplicates. Nested boxes can represent
    # legitimate panels, labels, or inset figures and must remain available.
    retained: list[tuple[int, Block]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (
            bool(item[1].markdown.strip()),
            len(item[1].markdown.strip()),
            len(item[1].provenance),
            len(item[1].metadata),
            _bbox_area(item[1].bbox),
        ),
        reverse=True,
    ):
        _, block = candidate
        if any(_iou(block.bbox, kept.bbox) >= 0.90 for _, kept in retained):
            rejected.add(candidate[0])
            warnings.append("visual_duplicate_figure_crop_rejected")
            continue
        retained.append(candidate)

    if rejected:
        blocks[:] = [block for index, block in enumerate(blocks) if index not in rejected]
    return sorted(set(warnings))


def _padded_bbox(
    bbox: tuple[float, float, float, float],
    *,
    padding: float = 8.0,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = bbox
    return (
        max(0.0, left - padding),
        max(0.0, top - padding),
        min(1000.0, right + padding),
        min(1000.0, bottom + padding),
    )


def _figure_crop_status(
    page_image: Image.Image,
    bbox: tuple[float, float, float, float],
) -> tuple[bool, bool]:
    width, height = page_image.size
    left, top, right, bottom = bbox
    crop = page_image.crop((
        max(0, round(left * width / 1000)),
        max(0, round(top * height / 1000)),
        min(width, round(right * width / 1000)),
        min(height, round(bottom * height / 1000)),
    ))
    crop.thumbnail((384, 384), Image.Resampling.LANCZOS)
    content = crop.point(lambda value: 255 if value < 255 else 0).getbbox()
    if content is None:
        return True, False
    touches_edge = (
        content[0] <= 1
        or content[1] <= 1
        or content[2] >= crop.width - 1
        or content[3] >= crop.height - 1
    )
    return False, touches_edge


def _bbox_area(bbox: tuple[float, float, float, float] | None) -> float:
    if not bbox:
        return 0.0
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


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
            if block.kind not in {"table", "figure"}
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
