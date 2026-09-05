from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from copy import deepcopy
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import Image
from tqdm.auto import tqdm

from .adapters import open_document
from .assets import AssetStore
from .chapters import detect_chapters
from .compare import compare_text
from .constants import AUTO_SPLIT_BYTES, DEFAULT_DPI, SCHEMA_VERSION, commit_version
from .embedded import assess_embedded, bbox_coverage, embedded_text_for_bbox, iter_embedded_characters
from .alignment import align_glyphs, delimiter_edits, math_font_role, script_edits
from .semantics import repair_accents, restore_inline_math
from .footnotes import normalize_footnotes
from .formatting import format_and_lint
from .mathlint import validator_identity
from .lists import normalize_lists, render_list
from .markdown import (
    merge_html_tables,
    normalize_table_blocks,
    strict_page_markdown,
    write_markdown,
)
from .model import Block, Comparison, EmbeddedEvidence, Link, OcrObservation, PageResult
from .native import observation_dict, parse_native_observation, reconcile_observations
from .ocr import MlxUnlimitedOcr, OcrBackend, confidence_summary, split_multi_page_output
from .quality import adjacent_overlap, output_quality_warnings, runaway_repetition_span
from .util import atomic_json, sha256_file, slugify
from .util import atomic_text
from .verify import verify_bundle

FIGURE_KINDS = {"figure", "image", "diagram", "chart", "graphic", "illustration", "photo", "map"}
FORMULA_KINDS = {"formula", "equation", "display_formula"}
EMBEDDED_PROOF_MARKS = {"□": r"\(\square\)", "∎": r"\(\blacksquare\)"}
REVIEW_METADATA_KEYS = {
    "review_required",
    "review_reason",
    "review_confidence",
    "review_consensus",
    "review_candidates",
    "review_base",
    "review_detail",
}


def _compare_with_embedded(visual: str, embedded: EmbeddedEvidence) -> Comparison:
    comparison = compare_text(visual, embedded.text)
    if embedded.extractor == "ignored":
        comparison.warnings = [
            warning
            for warning in comparison.warnings
            if warning != "embedded_text_absent"
        ]
    return comparison


def convert(
    source: Path,
    *,
    force: bool = False,
    backend: OcrBackend | None = None,
    ignore_embedded_text: bool = False,
) -> Path:
    """Convert a document, checkpointing pages beside the source before publishing."""
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
    workspace = _convert_workspace(
        source,
        _intermediate_root(source),
        backend=backend,
        force=force,
        ignore_embedded_text=ignore_embedded_text,
    )
    verification = verify_bundle(workspace)
    for warning in verification.warnings:
        print(f"pages2md: warning: {warning}", file=sys.stderr)
    return _publish_output(workspace, target, source_hash, version)


def _intermediate_root(source: Path) -> Path:
    """Return the persistent, private workspace directory beside ``source``."""
    source = source.resolve()
    legacy = source.with_suffix(".pages2md")
    legacy_metadata = _read_json(legacy / "metadata.json")
    if isinstance(legacy_metadata, dict) and legacy_metadata.get("source") == str(source):
        return legacy
    return source.with_name(f"{source.name}.pages2md")


def _convert_workspace(
    source: Path,
    output: Path,
    *,
    backend: OcrBackend | None = None,
    force: bool = False,
    resume: bool = True,
    ignore_embedded_text: bool = False,
) -> Path:
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    output = output.resolve()
    output_metadata = _read_json(output / "metadata.json")
    bundle = (
        output
        if isinstance(output_metadata, dict) and output_metadata.get("source") == str(source)
        else output / slugify(source.name, "document")
    )
    backend = backend or MlxUnlimitedOcr()
    ocr_fingerprint = {
        "contract_version": 1,
        "source_sha256": _source_hash(source),
        "backend": dict(backend.identity),
        "dpi": DEFAULT_DPI,
        "multi_page": True,
        "quality": "thorough",
    }
    assembly_fingerprint = {
        "split_mode": "auto",
        "math_validator": validator_identity(),
        "embedded_text": "ignored" if ignore_embedded_text else "enabled",
        "code": _code_fingerprint(
            "adapters.py", "assets.py", "chapters.py", "compare.py", "formatting.py",
            "embedded.py", "alignment.py", "lists.py", "markdown.py", "model.py", "native.py", "pipeline.py",
            "quality.py", "verify.py", "mathlint.py", "katex_lint.cjs", "urls.py", "semantics.py", "footnotes.py",
        ),
    }
    previous = _read_json(bundle / "metadata.json")
    progress_state = _read_json(bundle / "progress.json")
    resume_state = progress_state or previous
    can_reuse_ocr = bool(
        resume
        and resume_state
        and resume_state.get("source") == str(source)
        and resume_state.get("ocr_fingerprint") == ocr_fingerprint
    )
    can_resume_pages = bool(
        can_reuse_ocr
        and resume_state.get("assembly_fingerprint") == assembly_fingerprint
    )
    if bundle.exists() and not can_reuse_ocr:
        shutil.rmtree(bundle)
    if (
        can_resume_pages
        and resume_state.get("status") == "complete"
        and _page_checkpoints_complete(bundle, previous)
    ):
        verification = verify_bundle(bundle)
        if verification.ok:
            return bundle
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "pages").mkdir(exist_ok=True)
    (bundle / "raw").mkdir(exist_ok=True)
    work = bundle / ".work"
    work.mkdir(exist_ok=True)
    assets = AssetStore(bundle / "assets", load_existing=can_reuse_ocr)
    fingerprint = {"ocr": ocr_fingerprint, "assembly": assembly_fingerprint}
    started = time.time()
    _write_progress(
        bundle,
        source=source,
        fingerprint=fingerprint,
        completed_pages=set(),
        status="opening",
    )
    document = open_document(
        source,
        work,
        assets,
        dpi=DEFAULT_DPI,
        ignore_embedded_text=ignore_embedded_text,
    )

    resumed: dict[int, PageResult] = {}
    reassembled: dict[int, PageResult] = {}
    if can_resume_pages:
        for source_page in document.pages:
            page_path = bundle / "pages" / f"page-{source_page.number:04d}.json"
            value = _read_json(page_path)
            if not isinstance(value, dict) or value.get("number") != source_page.number:
                continue
            try:
                resumed[source_page.number] = _page_from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
    elif can_reuse_ocr:
        for source_page in document.pages:
            page_path = bundle / "pages" / f"page-{source_page.number:04d}.json"
            value = _read_json(page_path)
            if not isinstance(value, dict) or value.get("number") != source_page.number:
                continue
            try:
                result = _reassemble_cached_page(
                    source_page,
                    value,
                    bundle,
                    assets,
                    document.outline,
                )
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
            if result is not None:
                reassembled[source_page.number] = result
    completed_pages = set(resumed) | set(reassembled)
    _write_progress(
        bundle,
        source=source,
        fingerprint=fingerprint,
        completed_pages=completed_pages,
        status="running",
    )

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
            if page.number in resumed:
                page_results.append(resumed[page.number])
                progress.update()
                continue
            if page.number in reassembled:
                result = reassembled[page.number]
                assets.write_manifest()
                atomic_json(bundle / "pages" / f"page-{page.number:04d}.json", result.to_dict())
                page_results.append(result)
                progress.update()
                continue
            blank, ink_fraction = _is_visually_blank(page.image_path)
            if not blank:
                ocr_pages.append(page)
                continue
            result = _blank_page_result(page, ink_fraction)
            assets.write_manifest()
            atomic_json(bundle / "pages" / f"page-{page.number:04d}.json", result.to_dict())
            page_results.append(result)
            completed_pages.add(page.number)
            _write_progress(
                bundle,
                source=source,
                fingerprint=fingerprint,
                completed_pages=completed_pages,
                status="running",
            )
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
                        embedded=source_page.embedded,
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
                    assets.write_manifest()
                    atomic_json(page_path, result.to_dict())
                    page_results.append(result)
                    completed_pages.add(source_page.number)
                    _write_progress(
                        bundle,
                        source=source,
                        fingerprint=fingerprint,
                        completed_pages=completed_pages,
                        status="running",
                    )
                except Exception as error:
                    failed.append({"page": source_page.number, "error": str(error)})
                finally:
                    progress.update()

    if failed:
        shutil.rmtree(work, ignore_errors=True)
        _write_progress(
            bundle,
            source=source,
            fingerprint=fingerprint,
            completed_pages=completed_pages,
            status="failed",
            errors=[f"page {item['page']}: {item['error']}" for item in failed],
        )
        raise RuntimeError(f"{len(failed)} page(s) failed; intermediate bundle: {bundle}")

    page_results.sort(key=lambda item: item.number)
    normalize_footnotes(page_results, _semantic_math_projection)
    _normalize_document_blocks(page_results)
    _merge_continued_tables(page_results)
    for result in page_results:
        # List normalization renders from structured item bodies, not the
        # top-level OCR string. Reconcile those bodies once they exist.
        if assess_embedded(result.embedded, result.visual_markdown).geometric:
            result.warnings.extend(restore_inline_math(
                [b for b in result.blocks if isinstance(b.metadata.get("list"), dict)],
                result.embedded, _semantic_math_projection,
            ))
        _strip_review_metadata(result.blocks)
        normalize_table_blocks(result.blocks)
    available_pages = {page.number for page in page_results}
    for result in page_results:
        _apply_links_to_blocks(
            result.blocks,
            result.embedded.links,
            page_number=result.number,
        )
        result.visual_markdown = strict_page_markdown(result, document.outline)
        retained_warnings = [
            warning
            for warning in result.warnings
            if warning.startswith("visual_") or warning == "unresolved_internal_link"
        ]
        if result.generation.get("multi_page_recovery"):
            retained_warnings.append("multi_page_recovered_corrupt_segment")
        result.comparison = _compare_with_embedded(result.visual_markdown, result.embedded)
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
    if format_result.math_validation.diagnostics:
        warnings.append("latex_validation_findings")
    output_fingerprints = {path: sha256_file(bundle / path) for path in files}
    assets.write_manifest()
    document_json = {
        "schema_version": SCHEMA_VERSION,
        "source": str(source),
        "kind": document.kind,
        "ignore_embedded_text": ignore_embedded_text,
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
        "ignore_embedded_text": ignore_embedded_text,
        "page_count": len(page_results),
        "requested_page_count": len(document.pages),
        "model": dict(backend.identity),
        "multi_page": True,
        "quality": "thorough",
        "split": split,
        "markdown_files": files,
        "warnings": sorted(set(warnings)),
        "formatting": {
            "formatter": "mdformat==1.0.0 + mdformat-gfm==1.0.0 + mdformat-footnote==0.1.3",
            "linter": "pymarkdownlnt==0.9.38",
            "idempotent": format_result.idempotent,
            "lint_errors": format_result.lint_errors,
            "preservation_skips": format_result.preservation_skips,
        },
        "math_validation": asdict(format_result.math_validation),
        "output_fingerprints": output_fingerprints,
        "failed_pages": failed,
        "duration_seconds": round(time.time() - started, 3),
        "platform": platform.platform(),
    }
    atomic_json(bundle / "metadata.json", metadata)
    _write_log(bundle, metadata)
    shutil.rmtree(work, ignore_errors=True)
    _write_progress(
        bundle,
        source=source,
        fingerprint=fingerprint,
        completed_pages=completed_pages,
        status="assembled",
    )
    verification = verify_bundle(bundle)
    if not verification.ok:
        _write_progress(
            bundle,
            source=source,
            fingerprint=fingerprint,
            completed_pages=completed_pages,
            status="verification_failed",
            errors=verification.errors,
        )
        raise RuntimeError(
            f"bundle verification failed: {'; '.join(verification.errors)}; intermediate bundle: {bundle}"
        )
    _write_progress(
        bundle,
        source=source,
        fingerprint=fingerprint,
        completed_pages=completed_pages,
        status="complete",
    )
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
    embedded_trust = assess_embedded(
        source_page.embedded,
        _blocks_markdown(blocks, ""),
    )
    trusted_embedded = source_page.embedded if embedded_trust.geometric else None
    usable_embedded = source_page.embedded if embedded_trust.usable else EmbeddedEvidence()
    if source_page.embedded.extractor not in {None, "ignored"}:
        validation_warnings.append(f"visual_embedded_text_{embedded_trust.state}")
    validation_warnings.extend(_repair_runaway_repetition(blocks))
    validation_warnings.extend(
        _canonicalize_figure_blocks(
            blocks,
            source_page.image_path,
            trusted_embedded,
        )
    )
    validation_warnings.extend(
        _restore_embedded_proof_marks(
            blocks,
            trusted_embedded or EmbeddedEvidence(),
            source_page.number,
        )
    )
    validation_warnings.extend(_repair_embedded_digit_runs(blocks, usable_embedded))
    validation_warnings.extend(_repair_embedded_delimiters(blocks, usable_embedded))
    validation_warnings.extend(_repair_embedded_math_glyphs(blocks, usable_embedded))
    validation_warnings.extend(_repair_embedded_math_structure(blocks, usable_embedded))
    validation_warnings.extend(_repair_malformed_math_syntax(blocks))
    validation_warnings.extend(
        _repair_embedded_short_insertions(
            blocks,
            trusted_embedded or EmbeddedEvidence(),
        )
    )
    validation_warnings.extend(_restore_embedded_math_alphabets(blocks, trusted_embedded or EmbeddedEvidence()))
    validation_warnings.extend(_repair_embedded_word_tokens(blocks, usable_embedded))
    validation_warnings.extend(repair_accents(blocks, trusted_embedded or EmbeddedEvidence(), _semantic_math_projection))
    validation_warnings.extend(restore_inline_math(blocks, trusted_embedded or EmbeddedEvidence(), _semantic_math_projection))
    _strip_review_metadata(blocks)
    visual_markdown = _blocks_markdown(blocks, "")
    comparison = _compare_with_embedded(visual_markdown, source_page.embedded)
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


def _strip_review_metadata(blocks: list[Block]) -> None:
    """Keep OCR uncertainty internal instead of publishing review markers."""
    for block in blocks:
        for key in REVIEW_METADATA_KEYS:
            block.metadata.pop(key, None)


def _has_embedded_coverage_gap(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> bool:
    """Detect meaningful native text lines not covered by any visual OCR block."""
    uncovered_characters = 0
    visual_text = _coverage_text(" ".join(block.markdown for block in blocks))
    for native_block in embedded.blocks:
        for line in native_block.get("lines", []):
            text = re.sub(r"\s+", " ", str(line.get("text", ""))).strip()
            bbox = line.get("bbox", [])
            if len(text) < 8 or len(bbox) != 4:
                continue
            if bbox[1] < 35 or bbox[3] > 925:
                continue
            native_text = _coverage_text(text)
            if native_text and native_text in visual_text:
                continue
            covered = any(
                block.bbox is not None
                and (
                    bbox_coverage(bbox, _expand_bbox(block.bbox, 8.0)) >= 0.55
                    or bbox_coverage(block.bbox, _expand_bbox(tuple(bbox), 8.0)) >= 0.55
                )
                for block in blocks
            )
            if not covered:
                uncovered_characters += len(text)
                if uncovered_characters >= 18:
                    return True
    return False


def _coverage_text(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), re.UNICODE))


def _expand_bbox(bbox, padding: float):
    return (
        max(0.0, float(bbox[0]) - padding),
        max(0.0, float(bbox[1]) - padding),
        min(1000.0, float(bbox[2]) + padding),
        min(1000.0, float(bbox[3]) + padding),
    )


def _reassemble_cached_page(
    source_page,
    value: dict[str, Any],
    bundle: Path,
    assets: AssetStore,
    outline: list[dict[str, Any]],
) -> PageResult | None:
    """Re-run deterministic assembly from saved model observations."""
    generation = value.get("generation", {})
    if generation.get("mode") == "blank":
        return _blank_page_result(
            source_page,
            float(generation.get("ink_fraction", 0.0)),
        )
    raw = value.get("raw_ocr")
    visual = value.get("visual", {})
    multi_value = visual.get("multi_page") if isinstance(visual, dict) else None
    if not isinstance(raw, str) or not isinstance(multi_value, dict):
        return None
    group_observation = _cached_observation(multi_value, bundle)
    source_pages = generation.get("source_pages", [source_page.number])
    if not isinstance(source_pages, list) or not all(
        isinstance(number, int) for number in source_pages
    ):
        source_pages = [source_page.number]
    primary = parse_native_observation(
        raw,
        mode=str(generation.get("mode", "multi_base")),
        source_pages=source_pages,
        generation=generation,
    )
    _restore_cached_block_details(primary, group_observation)
    primary.id = group_observation.id
    candidate_values = visual.get("candidates", [])
    if not isinstance(candidate_values, list):
        return None
    candidates = [
        _cached_observation(candidate, bundle)
        for candidate in candidate_values
        if isinstance(candidate, dict)
    ]
    blocks, recovery, validation_warnings = reconcile_observations(
        primary,
        candidates,
        embedded_text=source_page.embedded.text,
        embedded=source_page.embedded,
    )
    return _page_result(
        source_page,
        primary,
        group_observation,
        candidates,
        blocks,
        recovery,
        validation_warnings,
        assets,
        outline,
    )


def _cached_observation(value: dict[str, Any], bundle: Path) -> OcrObservation:
    raw_path = value.get("raw_path")
    if not isinstance(raw_path, str):
        raise ValueError("cached observation has no raw path")
    path = (bundle / raw_path).resolve()
    if not path.is_relative_to(bundle.resolve()) or not path.is_file():
        raise ValueError("cached observation raw path is invalid")
    blocks_value = value.get("blocks", [])
    if not isinstance(blocks_value, list):
        raise ValueError("cached observation blocks are invalid")
    return OcrObservation(
        id=str(value["id"]),
        mode=str(value.get("mode", "multi_base")),
        raw=path.read_text(encoding="utf-8"),
        source_pages=[int(number) for number in value.get("source_pages", [])],
        generation=deepcopy(value.get("generation", {})),
        blocks=[_block_from_dict(block) for block in blocks_value],
        warnings=list(value.get("warnings", [])),
    )


def _restore_cached_block_details(
    primary: OcrObservation,
    group: OcrObservation,
) -> None:
    """Restore token confidence omitted from page-local raw checkpoints."""
    unused = set(range(len(group.blocks)))
    for block in primary.blocks:
        match = next(
            (
                index
                for index in unused
                if group.blocks[index].kind == block.kind
                and group.blocks[index].markdown == block.markdown
                and group.blocks[index].bbox == block.bbox
            ),
            None,
        )
        if match is None:
            continue
        cached = group.blocks[match]
        block.confidence = cached.confidence
        block.metadata = deepcopy(cached.metadata)
        unused.remove(match)


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
    comparison = _compare_with_embedded("", source_page.embedded)
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
    comparison = _compare_with_embedded(markdown, source_page.embedded)
    embedded_trust = assess_embedded(source_page.embedded, markdown)
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
    coverage_problem = bool(
        embedded_trust.geometric
        and _has_embedded_coverage_gap(primary.blocks, source_page.embedded)
    )
    structural_problem = bool(
        set(primary.warnings) & critical or group_problem or coverage_problem
    )
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
    ) or bool(
        coverage_problem
        and _has_embedded_coverage_gap(
            [*primary.blocks, *(candidates[0].blocks if candidates else [])],
            source_page.embedded,
        )
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
        body = [b for b in previous.blocks if not b.metadata.get("footnote")]
        if not body:
            continue
        left, right = body[-1], current.blocks[0]
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
            elif block.kind == "list" and isinstance(block.metadata.get("list"), dict):
                _clean_list_prose(block.metadata["list"])
                block.markdown = render_list(block.metadata["list"])

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


def _clean_list_prose(node: dict) -> None:
    """Apply the same prose cleanup inside lists, without touching equations."""
    for item in node.get("items", []):
        for block in item.get("blocks", []):
            if block.get("kind") == "paragraph":
                block["markdown"] = _clean_prose(block["markdown"])
        for child in item.get("children", []):
            _clean_list_prose(child)


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
    embedded: EmbeddedEvidence | None = None,
) -> list[str]:
    """Clamp figure boxes and reject blank, duplicate, or glyph-explained crops."""
    candidates: list[tuple[int, Block]] = []
    warnings: list[str] = []
    rejected: set[int] = set()
    proof_glyphs = list(_embedded_characters(embedded, EMBEDDED_PROOF_MARKS))
    with Image.open(page_image) as source:
        grayscale = source.convert("L")
        for index, block in enumerate(blocks):
            if block.kind not in FIGURE_KINDS or not block.bbox:
                continue
            matching_glyph = next(
                (
                    glyph
                    for glyph in proof_glyphs
                    if _small_box_contains(block.bbox, glyph["bbox"])
                ),
                None,
            )
            if matching_glyph is not None:
                block.kind = "paragraph"
                block.markdown = EMBEDDED_PROOF_MARKS[matching_glyph["text"]]
                block.bbox = tuple(matching_glyph["bbox"])
                block.metadata.update({
                    "embedded_glyph": matching_glyph["text"],
                    "reclassified_from": "figure",
                    "reclassification_reason": "pdf_text_glyph_geometry",
                })
                block.provenance.append({
                    "source": "embedded_pdf_glyph",
                    "font": matching_glyph.get("font", ""),
                    "bbox": matching_glyph["bbox"],
                })
                warnings.append("visual_text_glyph_figure_reclassified")
                continue
            block.bbox = _padded_bbox(block.bbox)
            blank, touches_edge = _figure_crop_status(grayscale, block.bbox)
            if blank:
                warnings.append("visual_blank_figure_crop_rejected")
                if block.markdown.strip():
                    block.kind = "paragraph"
                    block.bbox = None
                else:
                    rejected.add(index)
                continue
            if touches_edge:
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


def _embedded_characters(
    embedded: EmbeddedEvidence | None,
    selected: set[str] | dict[str, str] | None = None,
):
    for glyph in iter_embedded_characters(embedded):
        if selected is None or glyph["text"] in selected:
            yield glyph


def _small_box_contains(container, glyph) -> bool:
    width = max(0.0, container[2] - container[0])
    height = max(0.0, container[3] - container[1])
    if width > 90 or height > 90:
        return False
    center_x = (glyph[0] + glyph[2]) / 2
    center_y = (glyph[1] + glyph[3]) / 2
    return (
        container[0] - 8 <= center_x <= container[2] + 8
        and container[1] - 8 <= center_y <= container[3] + 8
    )


def _embedded_proof_anchor(blocks: list[Block], embedded: EmbeddedEvidence, glyph) -> Block | None:
    """Locate the ending text in PDF coordinates despite drift in OCR boxes."""
    box = glyph["bbox"]
    em = float((glyph.get("em") or [0, box[3] - box[1]])[1])
    baseline = (glyph.get("origin") or [0, box[3]])[1]
    region = (0, max(0, baseline - 1.5 * em), box[0], min(1000, baseline + .7 * em))
    candidates = []
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        if block.bbox[3] < baseline - 3 * em or block.bbox[1] > baseline + 3 * em:
            continue
        aligned = align_glyphs(block.markdown, embedded, region, _semantic_math_projection)
        equal = [(a, b) for a, b in aligned.matches.items() if aligned.text[a] == aligned.native[b]]
        if len(equal) < 6 or len(equal) / max(1, len(aligned.text)) < .7:
            continue
        last = aligned.glyphs[max(b for _, b in equal)]
        last_baseline = (last.get("origin") or [0, last["bbox"][3]])[1]
        if abs(last_baseline - baseline) > .7 * em:
            continue
        candidates.append((len(equal) / max(1, len(aligned.text)), block))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if candidates and (len(candidates) == 1 or candidates[0][0] - candidates[1][0] > .1):
        return candidates[0][1]
    return None


def _restore_embedded_proof_marks(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
    page: int,
) -> list[str]:
    """Normalize or insert PDF proof glyphs that OCR omitted from its block stream."""
    restored = False
    for glyph in _embedded_characters(embedded, EMBEDDED_PROOF_MARKS):
        rendered = EMBEDDED_PROOF_MARKS[glyph["text"]]
        anchor = _embedded_proof_anchor(blocks, embedded, glyph)
        overlapping = [
            (index, block)
            for index, block in enumerate(blocks)
            if block.bbox and _bbox_coverage(glyph["bbox"], block.bbox) >= 0.45
        ]
        existing = next(
            (
                block
                for _, block in overlapping
                if any(mark in block.markdown for mark in ("□", "☐", r"\square", r"\blacksquare"))
            ),
            None,
        )
        if existing is not None:
            updated = existing.markdown.replace("☐", rendered).replace("□", rendered)
            if updated != existing.markdown:
                existing.markdown = updated
                existing.metadata["normalized_embedded_proof_mark"] = glyph["text"]
                restored = True
            if anchor is not None and existing is not anchor and existing.markdown.strip() == rendered:
                old = blocks.index(existing)
                blocks.remove(existing)
                insertion = blocks.index(anchor) + 1
                blocks.insert(insertion, existing)
                restored |= old != insertion
            continue
        if any(
            block.metadata.get("embedded_glyph") == glyph["text"]
            and block.bbox
            and _bbox_coverage(glyph["bbox"], block.bbox) >= 0.45
            for block in blocks
        ):
            continue
        proof = Block(
            kind="paragraph",
            markdown=rendered,
            bbox=glyph["bbox"],
            confidence=1.0,
            source_pages=[page],
            provenance=[{
                "source": "embedded_pdf_glyph",
                "font": glyph.get("font", ""),
                "bbox": glyph["bbox"],
            }],
            metadata={
                "embedded_glyph": glyph["text"],
                "recovery_reason": "ocr_omitted_pdf_text_glyph",
            },
        )
        if anchor is not None:
            insertion = blocks.index(anchor) + 1
        elif overlapping:
            insertion = max(index for index, _ in overlapping) + 1
        else:
            insertion = next(
                (
                    index
                    for index, block in enumerate(blocks)
                    if block.bbox and block.bbox[1] > glyph["bbox"][3]
                ),
                len(blocks),
            )
        blocks.insert(insertion, proof)
        restored = True
    return ["visual_embedded_proof_mark_recovered"] if restored else []


def _repair_embedded_digit_runs(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Join OCR-split numeric literals only when matched PDF text confirms them."""
    repaired = False
    decimal = re.compile(r"(?<!\w)(\d+)\.\s+((?:\d\s+)+\d)(?!\w)")
    integer = re.compile(r"(?<![\w.])(\d(?:\s+\d){1,})(?![\w.])")
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        evidence = _embedded_text_for_bbox(embedded, block.bbox)
        if not evidence:
            continue
        evidence_compact = re.sub(r"\s+", "", evidence)
        changes: list[dict[str, str]] = []
        native_decimals = sorted(set(
            re.findall(r"(?<!\w)\d+\.\d{3,}(?!\w)", evidence_compact)
        ))

        def join_decimal(match: re.Match[str]) -> str:
            candidate = match.group(1) + "." + re.sub(r"\s+", "", match.group(2))
            replacement = candidate
            if candidate not in evidence_compact:
                compatible = [
                    native
                    for native in native_decimals
                    if native.split(".", 1)[0] == match.group(1)
                    and abs(len(native) - len(candidate)) <= 1
                    and SequenceMatcher(None, candidate, native, autojunk=False).ratio() >= 0.72
                ]
                if len(compatible) != 1:
                    return match.group(0)
                replacement = compatible[0]
            changes.append({"visual": match.group(0), "embedded": replacement})
            return replacement

        def join_integer(match: re.Match[str]) -> str:
            candidate = re.sub(r"\s+", "", match.group(1))
            if len(candidate) < 3:
                left = match.string[max(0, match.start() - 3) : match.start()]
                right = match.string[match.end() : match.end() + 3]
                if "}" not in right or not any(marker in left for marker in ("{", "/", "^", "_")):
                    return match.group(0)
            if candidate not in evidence_compact:
                return match.group(0)
            changes.append({"visual": match.group(0), "embedded": candidate})
            return candidate

        updated = decimal.sub(join_decimal, block.markdown)
        updated = integer.sub(join_integer, updated)
        if updated != block.markdown:
            block.markdown = updated
            block.metadata.setdefault("embedded_text_repairs", []).extend(changes)
            repaired = True
    return ["visual_embedded_numeric_repair"] if repaired else []


def _repair_embedded_word_tokens(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Repair close lexical OCR substitutions while preserving Markdown and math."""
    from difflib import SequenceMatcher

    repaired = False
    for block in blocks:
        if not block.bbox or block.kind not in {
            "paragraph", "text", "caption", "heading", "title", "reference", "ref_text",
        }:
            continue
        evidence = _embedded_text_for_bbox(embedded, block.bbox)
        if not evidence:
            continue
        visual_tokens = _unprotected_word_tokens(block.markdown)
        embedded_tokens = [
            (match.group(0), match.start(), match.end())
            for match in re.finditer(r"[^\W_]+", evidence, re.UNICODE)
        ]
        if not visual_tokens or not embedded_tokens:
            continue
        matcher = SequenceMatcher(
            None,
            [token[0].casefold() for token in visual_tokens],
            [token[0].casefold() for token in embedded_tokens],
            autojunk=False,
        )
        replacements: list[tuple[int, int, str, str]] = []
        for operation, visual_start, visual_end, embedded_start, embedded_end in matcher.get_opcodes():
            if operation != "replace" or visual_end - visual_start != embedded_end - embedded_start:
                continue
            for visual, native in zip(
                visual_tokens[visual_start:visual_end],
                embedded_tokens[embedded_start:embedded_end],
            ):
                if not _safe_embedded_token_repair(visual[0], native[0]):
                    continue
                replacements.append((visual[1], visual[2], visual[0], native[0]))
        if not replacements:
            continue
        markdown = block.markdown
        changes: list[dict[str, str]] = []
        for start, end, visual, native in reversed(replacements):
            markdown = markdown[:start] + native + markdown[end:]
            changes.append({"visual": visual, "embedded": native})
        block.markdown = markdown
        block.metadata.setdefault("embedded_text_repairs", []).extend(reversed(changes))
        repaired = True
    return ["visual_embedded_lexical_repair"] if repaired else []


def _repair_embedded_short_insertions(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Restore short omissions bracketed by trusted PDF text and OCR uncertainty."""
    repaired = False
    document_markdown = "\n".join(block.markdown for block in blocks)
    for block in blocks:
        if not block.bbox or block.kind not in {
            "paragraph", "text", "caption", "heading", "title", "reference", "ref_text",
        }:
            continue
        uncertain = [
            span
            for span in block.metadata.get("uncertain_spans", [])
            if float(span.get("confidence", 1.0)) <= 0.75
        ]
        if not uncertain:
            continue
        evidence = _embedded_text_for_bbox(embedded, block.bbox)
        if not evidence:
            continue
        visual, visual_map = _alignment_projection(block.markdown)
        native, native_map = _alignment_projection(evidence)
        if not visual or not native:
            continue
        opcodes = SequenceMatcher(None, visual, native, autojunk=False).get_opcodes()
        insertions: list[tuple[int, str, str]] = []
        for opcode_index, (operation, left_start, left_end, right_start, right_end) in enumerate(opcodes):
            if operation != "insert" or left_start != left_end or right_start == right_end:
                continue
            if opcode_index == 0 or opcode_index + 1 >= len(opcodes):
                continue
            before = opcodes[opcode_index - 1]
            after = opcodes[opcode_index + 1]
            if before[0] != "equal" or after[0] != "equal":
                continue
            if before[2] - before[1] < 8 or after[2] - after[1] < 8:
                continue
            markdown_index = (
                visual_map[left_start]
                if left_start < len(visual_map)
                else len(block.markdown)
            )
            if not _near_uncertain_span(markdown_index, uncertain):
                continue
            native_start = native_map[right_start]
            native_end = native_map[right_end - 1] + 1
            inserted = re.sub(r"\s+", " ", evidence[native_start:native_end])
            left_neighbor = visual[left_start - 1] if left_start else ""
            right_neighbor = visual[left_start] if left_start < len(visual) else ""
            if not _safe_embedded_insertion(
                inserted,
                document_markdown,
                left_neighbor=left_neighbor,
                right_neighbor=right_neighbor,
            ):
                continue
            inserted = _format_embedded_inserted_math(inserted, embedded, block.bbox)
            insertions.append((markdown_index, inserted, evidence[native_start:native_end]))
        if not insertions:
            continue
        markdown = block.markdown
        changes: list[dict[str, str]] = []
        for index, inserted, source in reversed(insertions):
            markdown = markdown[:index] + inserted + markdown[index:]
            changes.append({"visual": "", "embedded": source})
        block.markdown = markdown
        block.metadata.setdefault("embedded_text_repairs", []).extend(reversed(changes))
        block.metadata["embedded_supported_insertion"] = True
        if all(
            any(_near_uncertain_span(index, [span]) for index, _, _ in insertions)
            for span in uncertain
        ):
            block.metadata.pop("uncertain_spans", None)
        repaired = True
    return ["visual_embedded_insertion_repair"] if repaired else []


def _alignment_projection(value: str) -> tuple[str, list[int]]:
    """Return comparable visible text plus source offsets for safe splice points."""
    projected: list[tuple[str, int]] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\":
            if index + 1 < len(value) and value[index + 1] in "()[]{}":
                index += 2
                projected.append((" ", index - 1))
                continue
            command = re.match(r"\\[A-Za-z]+", value[index:])
            if command:
                index += len(command.group(0))
                projected.append((" ", index - 1))
                continue
            index += 1
            continue
        normalized = unicodedata.normalize("NFKC", character)
        for visible in normalized:
            if visible in "{}_^$*`":
                projected.append((" ", index))
            elif visible.isprintable():
                projected.append((" " if visible.isspace() else visible.casefold(), index))
        index += 1
    collapsed: list[str] = []
    offsets: list[int] = []
    for character, source_index in projected:
        if character == " ":
            if not collapsed or collapsed[-1] == " ":
                continue
        collapsed.append(character)
        offsets.append(source_index)
    while collapsed and collapsed[-1] == " ":
        collapsed.pop()
        offsets.pop()
    return "".join(collapsed), offsets


def _near_uncertain_span(index: int, spans: list[dict[str, Any]]) -> bool:
    for span in spans:
        start = int(span.get("start", 0))
        end = int(span.get("end", start))
        distance = 0 if start <= index <= end else min(abs(index - start), abs(index - end))
        if distance <= 16:
            return True
    return False


def _safe_embedded_insertion(
    inserted: str,
    document_markdown: str,
    *,
    left_neighbor: str,
    right_neighbor: str,
) -> bool:
    plain = inserted.strip()
    words = re.findall(r"[^\W_]+", plain, re.UNICODE)
    spelling_character = bool(
        len(plain) == 1
        and plain.isalpha()
        and left_neighbor.isalpha()
        and right_neighbor.isalpha()
    )
    if not spelling_character and (len(words) < 2 or len(words) > 4 or len(plain) > 32):
        return False
    if any(not (character.isalnum() or character.isspace() or character in "-'’") for character in plain):
        return False
    identifiers = [word for word in words if len(word) == 1 and word.isascii() and word.isupper()]
    return all(
        re.search(
            rf"(?:\\(?:mathcal|mathbf|mathbb)\s*\{{\s*{letter}\s*\}}|(?<!\w){letter}(?!\w))",
            document_markdown,
        )
        for letter in identifiers
    )


def _format_embedded_inserted_math(
    inserted: str,
    embedded: EmbeddedEvidence,
    bbox: tuple[float, float, float, float],
) -> str:
    block = Block("paragraph", inserted, bbox=bbox)
    _restore_embedded_math_alphabets([block], embedded)
    return block.markdown


def _repair_embedded_delimiters(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Repair Hasse-derivative brackets when the PDF glyph stream confirms them."""
    import unicodedata

    patterns = [
        re.compile(r"\^\s*\{\s*\\\{\s*([A-Za-z0-9]+)\s*\\\}\s*\}"),
        re.compile(r"\^\s*\{\s*\\lfloor\s*([A-Za-z0-9]+)\s*\\rfloor\s*\}"),
        re.compile(r"\^\s*\{\s*\[\s*([A-Za-z0-9]+)\s*\]\s*\}"),
    ]
    repaired = False
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        evidence = unicodedata.normalize(
            "NFKC", _embedded_text_for_bbox(embedded, block.bbox)
        )
        evidence = re.sub(r"\s+", "", evidence)
        if not evidence:
            continue
        changes: list[dict[str, str]] = []

        def replace(match: re.Match[str]) -> str:
            bracketed = f"[{match.group(1)}]"
            if bracketed not in evidence:
                return match.group(0)
            replacement = f"^ {{{bracketed}}}"
            changes.append({"visual": match.group(0), "embedded": replacement})
            return replacement

        markdown = block.markdown
        for pattern in patterns:
            markdown = pattern.sub(replace, markdown)
        if markdown != block.markdown:
            block.markdown = markdown
            block.metadata.setdefault("embedded_text_repairs", []).extend(changes)
            repaired = True
    return ["visual_embedded_delimiter_repair"] if repaired else []


_MATH_COMMAND_CHARACTERS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊",
    "rfloor": "⌋", "in": "∈", "equiv": "≡", "approx": "≈",
    "pm": "±", "times": "×", "cdot": "·", "sum": "∑", "prod": "∏",
    "mid": "|", "vert": "|",
}
_MATH_CHARACTER_COMMANDS = {
    character: command
    for command, character in _MATH_COMMAND_CHARACTERS.items()
    if command not in {"epsilon", "vartheta", "varphi", "le", "ge", "vert"}
}
_MATH_LAYOUT_COMMANDS = {
    "left", "right", "big", "Big", "bigg", "Bigg", "mathcal", "mathbb",
    "mathbf", "mathrm", "mathsf", "operatorname", "text", "begin", "end",
    "array", "tag", "quad", "qquad", "displaystyle",
}


def _semantic_math_projection(value: str) -> tuple[str, list[tuple[int, int]]]:
    """Project TeX or PDF Unicode text to comparable mathematical characters."""
    projected: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\":
            if index + 1 < len(value) and value[index + 1] in "{}[]()|":
                if value[index + 1] in "{}|":
                    projected.append(value[index + 1])
                    spans.append((index, index + 2))
                index += 2
                continue
            command = re.match(r"\\([A-Za-z]+)", value[index:])
            if command:
                name = command.group(1)
                end = index + len(command.group(0))
                if name in _MATH_COMMAND_CHARACTERS:
                    projected.append(_MATH_COMMAND_CHARACTERS[name])
                    spans.append((index, end))
                elif name in {"dots", "ldots", "cdots"}:
                    projected.append("…")
                    spans.append((index, end))
                index = end
                continue
            index += 1
            continue
        normalized = unicodedata.normalize("NFKC", character)
        for visible in normalized:
            if visible.isspace() or visible in "{}_^$*`®©ª«¬︁︂︃︄":
                continue
            if visible in "−–—":
                visible = "-"
            if visible.isprintable():
                projected.append(visible)
                spans.append((index, index + 1))
        index += 1
    return "".join(projected), spans


def _math_source_ranges(markdown: str) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for match in _PROTECTED_MARKDOWN.finditer(markdown)
        if match.group(0).startswith((r"\(", r"\[", "$$", "$"))
    ]


def _math_replacement(character: str) -> str | None:
    if character in _MATH_CHARACTER_COMMANDS:
        return rf"\{_MATH_CHARACTER_COMMANDS[character]}"
    if character == "{":
        return r"\{"
    if character == "}":
        return r"\}"
    if len(character) == 1 and (character.isalnum() or character in "-+=<>[]()|,.;:"):
        return character
    return None


def _math_character_family(character: str) -> str:
    if character.isalpha():
        return "letter"
    if character.isdigit():
        return "digit"
    category = unicodedata.category(character)
    if category in {"Ps", "Pe"}:
        return category
    if category in {"Sm", "Pd"} or character in "+-=<>≤≥|":
        return "operator"
    return category


def _repair_embedded_math_glyphs(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Repair isolated math glyph substitutions with strong local PDF anchors."""
    repaired = False
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        ranges = _math_source_ranges(block.markdown)
        if not ranges:
            continue
        evidence = _embedded_text_for_bbox(embedded, block.bbox)
        if not evidence:
            continue
        alignment = align_glyphs(block.markdown, embedded, block.bbox, _semantic_math_projection)
        visual, visual_spans = alignment.text, alignment.spans
        native = alignment.native
        if not native:
            native, _ = _semantic_math_projection(evidence)
        if not visual or not native:
            continue
        opcodes = SequenceMatcher(None, visual, native, autojunk=False).get_opcodes()
        substitutions = []
        if alignment.native:
            for left, right in alignment.matches.items():
                if visual[left] == native[right]:
                    continue
                anchors = []
                for direction in (-1, 1):
                    length = 0
                    cursor = left + direction
                    while cursor in alignment.matches and visual[cursor] == native[alignment.matches[cursor]]:
                        length += 1
                        cursor += direction
                    anchors.append(length)
                substitutions.append((left, left + 1, right, right + 1, *anchors))
        else:
            for index, (operation, left_start, left_end, right_start, right_end) in enumerate(opcodes):
                if operation != "replace" or left_end - left_start != 1 or right_end - right_start != 1:
                    continue
                before = opcodes[index - 1] if index else None
                after = opcodes[index + 1] if index + 1 < len(opcodes) else None
                substitutions.append((left_start, left_end, right_start, right_end,
                                      before[2] - before[1] if before and before[0] == "equal" else 0,
                                      after[2] - after[1] if after and after[0] == "equal" else 0))
        replacements: list[tuple[int, int, str, str]] = []
        for left_start, left_end, right_start, right_end, left_anchor, right_anchor in substitutions:
            source_start, source_end = visual_spans[left_start]
            if not any(start <= source_start and source_end <= end for start, end in ranges):
                continue
            uncertain = [
                span for span in block.metadata.get("uncertain_spans", [])
                if float(span.get("confidence", 1.0)) <= 0.75
            ]
            near_uncertain = _near_uncertain_span(source_start, uncertain)
            required_anchor = 4 if near_uncertain or block.kind in FORMULA_KINDS else 6
            if min(left_anchor, right_anchor) < 1 or left_anchor + right_anchor < required_anchor:
                continue
            visual_character = visual[left_start]
            native_character = native[right_start]
            if _math_character_family(visual_character) != _math_character_family(native_character):
                continue
            replacement = _math_replacement(native_character)
            if replacement is None or replacement == block.markdown[source_start:source_end]:
                continue
            if visual_character.isdigit() and native_character.isdigit():
                continue
            if (
                visual_character.isascii() and visual_character.isalpha()
                and native_character.isascii() and native_character.isalpha()
                and ((source_start and block.markdown[source_start - 1].isalpha())
                     or (source_end < len(block.markdown) and block.markdown[source_end].isalpha()))
                and not block.markdown[source_start:source_end].startswith("\\")
            ):
                continue
            replacements.append((source_start, source_end, block.markdown[source_start:source_end], replacement))
        if not replacements:
            continue
        markdown = block.markdown
        changes: list[dict[str, str]] = []
        for start, end, visual_source, replacement in sorted(replacements, reverse=True):
            markdown = markdown[:start] + replacement + markdown[end:]
            changes.append({"visual": visual_source, "embedded": replacement})
        block.markdown = markdown
        block.metadata.setdefault("embedded_text_repairs", []).extend(reversed(changes))
        repaired = True
    return ["visual_embedded_math_glyph_repair"] if repaired else []


def _embedded_spans_for_bbox(
    embedded: EmbeddedEvidence,
    bbox: tuple[float, float, float, float],
) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    for embedded_block in embedded.blocks:
        for line in embedded_block.get("lines", []):
            selected: list[dict[str, Any]] = []
            for span in line.get("spans", []):
                span_bbox = span.get("bbox")
                if not span_bbox and span.get("chars"):
                    boxes = [char.get("bbox") for char in span["chars"] if len(char.get("bbox", [])) == 4]
                    if boxes:
                        span_bbox = [
                            min(box[0] for box in boxes), min(box[1] for box in boxes),
                            max(box[2] for box in boxes), max(box[3] for box in boxes),
                        ]
                if len(span_bbox or []) != 4 or _bbox_coverage(span_bbox, bbox) < 0.45:
                    continue
                selected.append({**span, "bbox": tuple(float(item) for item in span_bbox)})
            if selected:
                lines.append(selected)
    return lines


def _repair_embedded_math_structure(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Use PDF geometry for scripts and paired delimiters lost by linear OCR."""
    repaired = False
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS or not _math_source_ranges(block.markdown):
            continue
        evidence = _embedded_text_for_bbox(embedded, block.bbox)
        if not evidence:
            continue
        spans_by_line = _embedded_spans_for_bbox(embedded, block.bbox)
        replacements: list[tuple[int, int, str, str]] = []
        alignment = align_glyphs(block.markdown, embedded, block.bbox, _semantic_math_projection)
        structured = script_edits(block.markdown, alignment)
        if structured:
            changes = []
            for start, end, replacement in sorted(structured, reverse=True):
                changes.append({"visual": block.markdown[start:end], "embedded": replacement})
                block.markdown = block.markdown[:start] + replacement + block.markdown[end:]
            block.metadata.setdefault("embedded_text_repairs", []).extend(reversed(changes))
            repaired = True

        if structured:
            alignment = align_glyphs(block.markdown, embedded, block.bbox, _semantic_math_projection)
        for start, end, replacement in delimiter_edits(
            block.markdown, alignment, _math_source_ranges(block.markdown)
        ):
            replacements.append((start, end, block.markdown[start:end], replacement))

        # Distinguish superscripts from subscripts by their position relative to
        # the base glyph, even when the PDF's linear text order interleaves them.
        script_pattern = re.compile(
            r"(?P<base>[A-Za-z])\s*_\s*\{\s*(?P<sub>(?:[^{}\n]|\{[^{}\n]*\})+)\}\s*"
            r"\^\s*\{\s*(?P<sup>(?:[^{}\n]|\{[^{}\n]*\})+)\}"
        )
        embedded_glyphs = [
            glyph
            for glyph in _embedded_characters(embedded)
            if _bbox_coverage(glyph["bbox"], block.bbox) >= 0.45
        ]
        for match in script_pattern.finditer(block.markdown):
            base = match.group("base")
            visual_sub, _ = _semantic_math_projection(match.group("sub"))
            visual_sup, _ = _semantic_math_projection(match.group("sup"))
            candidates: set[tuple[str, str]] = set()
            for glyph in embedded_glyphs:
                glyph_text = unicodedata.normalize("NFKC", str(glyph["text"]))
                base_size = float(glyph.get("size") or 0)
                if glyph_text != base or base_size <= 0:
                    continue
                gx0, gy0, gx1, gy1 = glyph["bbox"]
                base_center = (gy0 + gy1) / 2
                next_main_x = min(
                    (
                        other["bbox"][0]
                        for other in embedded_glyphs
                        if other["bbox"][0] > gx1 - 1
                        and float(other.get("size") or 0) >= base_size * 0.90
                        and str(other["text"]).strip()
                        and abs((other["bbox"][1] + other["bbox"][3]) / 2 - base_center) <= 8
                    ),
                    default=gx1 + 32,
                )
                upper: list[tuple[float, str]] = []
                lower: list[tuple[float, str]] = []
                for other in embedded_glyphs:
                    size = float(other.get("size") or base_size)
                    ox0, oy0, ox1, oy1 = other["bbox"]
                    center = (oy0 + oy1) / 2
                    if (
                        size >= base_size * 0.90
                        or ox1 < gx1 - 2
                        or ox0 >= next_main_x
                        or abs(center - base_center) > 10
                        or not str(other["text"]).strip()
                    ):
                        continue
                    text = unicodedata.normalize("NFKC", str(other["text"]))
                    if center < base_center - 1:
                        upper.append((ox0, text))
                    elif center > base_center + 1:
                        lower.append((ox0, text))
                native_sup = "".join(text for _, text in sorted(upper))
                native_sub = "".join(text for _, text in sorted(lower))
                if native_sup and native_sub:
                    candidates.add((native_sub, native_sup))
            supported = [
                native_sup
                for native_sub, native_sup in candidates
                if native_sub == visual_sub
                and native_sup != visual_sup
                and len(native_sup) == len(visual_sup)
                and SequenceMatcher(None, visual_sup, native_sup, autojunk=False).ratio() >= 0.50
                and re.fullmatch(r"[A-Za-z0-9]+", native_sup)
            ]
            if len(set(supported)) == 1:
                target = supported[0]
                source = match.group("sup")
                projected, projected_spans = _semantic_math_projection(source)
                differences = [
                    opcode
                    for opcode in SequenceMatcher(None, projected, target, autojunk=False).get_opcodes()
                    if opcode[0] != "equal"
                ]
                if len(differences) != 1:
                    continue
                operation, left_start, left_end, right_start, right_end = differences[0]
                if operation != "replace" or left_end - left_start != 1 or right_end - right_start != 1:
                    continue
                local_start, local_end = projected_spans[left_start]
                replacement = source[:local_start] + target[right_start] + source[local_end:]
                start, end = match.span("sup")
                replacements.append((start, end, match.group("sup"), replacement))

        # A visually raised trailing symbol belongs inside the preceding exponent.
        exponent_tail = re.compile(
            r"(?P<base>[A-Za-z])\s*\^\s*\{(?P<exponent>[^{}\n]{1,24})\}\s+(?P<tail>[A-Za-z])(?!\w)"
        )
        for match in exponent_tail.finditer(block.markdown):
            target, _ = _semantic_math_projection(match.group("exponent") + match.group("tail"))
            base = match.group("base")
            supported = False
            for line in spans_by_line:
                for index, span in enumerate(line):
                    span_text, _ = _semantic_math_projection(str(span.get("text", "")))
                    base_size = float(span.get("size") or 0)
                    if not span_text.endswith(base) or base_size <= 0:
                        continue
                    raised = ""
                    for following in line[index + 1:index + 6]:
                        size = float(following.get("size") or base_size)
                        if size >= base_size * 0.90:
                            break
                        text, _ = _semantic_math_projection(str(following.get("text", "")))
                        raised += text
                        if len(raised) >= len(target):
                            break
                    if raised.startswith(target):
                        supported = True
                        break
                if supported:
                    break
            if supported:
                replacement = f"{base} ^ {{{match.group('exponent').rstrip()} {match.group('tail')}}}"
                replacements.append((match.start(), match.end(), match.group(0), replacement))

        # Repair a sum binder when the body consistently uses another variable and
        # the embedded layer explicitly contains that alternate lower limit.
        sum_binder = re.compile(
            r"\\sum_\s*(?:\{\s*)?(?P<variable>[A-Za-z])\s*=\s*"
            r"(?P<lower>[A-Za-z0-9+\-]+)(?:\s*\})?\s*\^\s*(?:\{[^{}]+\}|[A-Za-z0-9]+)"
        )
        evidence_projection, _ = _semantic_math_projection(evidence)
        for match in sum_binder.finditer(block.markdown):
            body = block.markdown[match.end():match.end() + 180]
            candidates = [
                item.group(1) or item.group(2)
                for item in re.finditer(
                    r"(?:\^|_)\s*(?:\{\s*([A-Za-z])(?:\s*\+\s*\d+)?\s*\}|([A-Za-z]))",
                    body,
                )
            ]
            counts = {candidate: candidates.count(candidate) for candidate in set(candidates)}
            alternatives = [
                candidate for candidate, count in counts.items()
                if candidate != match.group("variable") and count >= 2
            ]
            if len(alternatives) != 1:
                continue
            alternative = alternatives[0]
            lower, _ = _semantic_math_projection(match.group("lower"))
            if f"{alternative}={lower}" not in evidence_projection:
                continue
            start, end = match.span("variable")
            replacements.append((start, end, match.group("variable"), alternative))

        # Recover a stacked relation label from small glyphs directly above '='.
        stackrel = re.compile(
            r"\\stackrel\s*\{\s*\\mathrm\s*\{\s*(?P<letter>[A-Za-z])\s*\}\s*"
            r"(?P<operator>[=+\-])\s*(?P<number>\d+)\s*\}\s*\{\s*=\s*\}"
        )
        for match in stackrel.finditer(block.markdown):
            labels: set[str] = set()
            all_spans = [span for line in spans_by_line for span in line]
            for equal in all_spans:
                equal_text, _ = _semantic_math_projection(str(equal.get("text", "")))
                equal_size = float(equal.get("size") or 0)
                if equal_text != "=" or equal_size <= 0:
                    continue
                ex0, ey0, ex1, ey1 = equal["bbox"]
                equal_center = (ey0 + ey1) / 2
                parts = []
                for span in all_spans:
                    size = float(span.get("size") or equal_size)
                    sx0, sy0, sx1, sy1 = span["bbox"]
                    center = (sy0 + sy1) / 2
                    if (
                        size < equal_size * 0.90
                        and equal_center - 15 <= center < equal_center - 1
                        and sx1 >= ex0 - 12 and sx0 <= ex1 + 12
                    ):
                        text, _ = _semantic_math_projection(str(span.get("text", "")))
                        parts.append((sx0, text))
                label = "".join(text for _, text in sorted(parts))
                if re.fullmatch(r"[A-Za-z][=+\-]\d+", label):
                    labels.add(label)
            if len(labels) != 1:
                continue
            label = next(iter(labels))
            expected_prefix = match.group("letter")
            expected_suffix = match.group("number")
            if not label.startswith(expected_prefix) or not label.endswith(expected_suffix):
                continue
            operator = label[len(expected_prefix):-len(expected_suffix)]
            if operator == match.group("operator"):
                continue
            start, end = match.span("operator")
            replacements.append((start, end, match.group("operator"), operator))

        if not replacements:
            continue
        markdown = block.markdown
        changes: list[dict[str, str]] = []
        non_overlapping: list[tuple[int, int, str, str]] = []
        for candidate in sorted(replacements, key=lambda item: (item[0], item[1])):
            if non_overlapping and candidate[0] < non_overlapping[-1][1]:
                continue
            non_overlapping.append(candidate)
        for start, end, visual_source, replacement in reversed(non_overlapping):
            markdown = markdown[:start] + replacement + markdown[end:]
            changes.append({"visual": visual_source, "embedded": replacement})
        block.markdown = markdown
        block.metadata.setdefault("embedded_text_repairs", []).extend(reversed(changes))
        repaired = True
    return ["visual_embedded_math_structure_repair"] if repaired else []


def _repair_malformed_math_syntax(blocks: list[Block]) -> list[str]:
    """Remove universally empty TeX scripts when a real script follows."""
    repaired = False
    pattern = re.compile(r"\^\s*\{\s*\}\s*(?=\^)")
    for block in blocks:
        if block.kind in FIGURE_KINDS or not _math_source_ranges(block.markdown):
            continue
        markdown, count = pattern.subn("", block.markdown)
        if not count:
            continue
        block.markdown = markdown
        block.metadata.setdefault("syntax_repairs", []).append("removed_empty_duplicate_exponent")
        repaired = True
    return ["visual_math_syntax_repair"] if repaired else []


def _restore_embedded_math_alphabets(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Restore styled capitals by aligning each OCR occurrence to one PDF glyph."""
    repaired = False
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        visual = _visible_uppercase_occurrences(block.markdown)
        alignment = align_glyphs(block.markdown, embedded, block.bbox, _semantic_math_projection)
        by_start = {start: index for index, (start, _) in enumerate(alignment.spans)}
        aligned: list[tuple[dict[str, object], dict[str, object], bool]] = []
        for occurrence in visual:
            index = by_start.get(occurrence["start"])
            native_index = alignment.matches.get(index)
            if native_index is None:
                continue
            glyph = alignment.glyphs[native_index]
            aligned.append((occurrence, {**glyph, "match_letter": glyph["letter"]},
                            occurrence["letter"] == glyph["letter"]))

        replacements: list[tuple[int, int, str, str]] = []
        for occurrence, glyph, same_letter in aligned:
            role = math_font_role(glyph)
            style = occurrence.get("style")
            target_letter = str(glyph["match_letter"] if not same_letter else occurrence["letter"])
            if not same_letter and role not in {"mathcal", "mathbb"}:
                continue
            if role in {"mathcal", "mathbb"} and (style != role or not same_letter):
                replacement = rf"\{role} {{{target_letter}}}"
            elif role == "ordinary" and style in {"mathcal", "mathbb"}:
                replacement = target_letter
            else:
                continue
            start = int(occurrence.get("style_start", occurrence["start"]))
            end = int(occurrence.get("style_end", occurrence["end"]))
            prefix = block.markdown[:start].rstrip()
            if not occurrence["math"]:
                # Only a standalone, exactly aligned symbol may gain math delimiters.
                if (not same_letter or role not in {"mathcal", "mathbb"}
                    or (start and block.markdown[start - 1].isalnum())
                    or (end < len(block.markdown) and block.markdown[end].isalnum())):
                    continue
                replacement = rf"\({replacement}\)"
            if role in {"mathcal", "mathbb"} and prefix.endswith(("^", "_")):
                replacement = "{" + replacement + "}"
            replacements.append((
                start,
                end,
                block.markdown[start:end],
                replacement,
            ))
        if not replacements:
            continue
        markdown = block.markdown
        changes: list[dict[str, str]] = []
        for start, end, visual_source, replacement in sorted(replacements, reverse=True):
            markdown = markdown[:start] + replacement + markdown[end:]
            changes.append({"visual": visual_source, "embedded": replacement})
        block.markdown = markdown
        block.metadata.setdefault("embedded_text_repairs", []).extend(reversed(changes))
        repaired = True
    return ["visual_embedded_math_alphabet_repair"] if repaired else []


def _visible_uppercase_occurrences(markdown: str) -> list[dict[str, object]]:
    command_ranges = [
        (match.start(), match.end())
        for match in re.finditer(r"\\[A-Za-z]+", markdown)
    ]
    math_ranges = [
        (match.start(), match.end())
        for match in _PROTECTED_MARKDOWN.finditer(markdown)
        if match.group(0).startswith((r"\(", r"\[", "$$", "$"))
    ]
    output: list[dict[str, object]] = []
    for match in re.finditer(r"[A-Z]", markdown):
        if any(start <= match.start() < end for start, end in command_ranges):
            continue
        prefix = markdown[: match.start()]
        styled = re.search(
            r"\\(mathcal|mathbb|mathbf|mathrm|mathsf)\s*\{\s*$",
            prefix,
        )
        style_end = match.end()
        if styled:
            closing = re.match(r"\s*\}", markdown[match.end():])
            if closing:
                style_end = match.end() + closing.end()
        output.append({
            "letter": match.group(0),
            "start": match.start(),
            "end": match.end(),
            "style": styled.group(1) if styled else None,
            "style_start": styled.start() if styled else match.start(),
            "style_end": style_end,
            "math": any(start <= match.start() < end for start, end in math_ranges),
        })
    return output


def _unprotected_word_tokens(markdown: str) -> list[tuple[str, int, int]]:
    protected = [(match.start(), match.end()) for match in _PROTECTED_MARKDOWN.finditer(markdown)]
    output: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^\W_]+", markdown, re.UNICODE):
        if match.start() and markdown[match.start() - 1] == "\\":
            continue
        if any(start <= match.start() < end for start, end in protected):
            continue
        output.append((match.group(0), match.start(), match.end()))
    return output


def _safe_embedded_token_repair(visual: str, embedded: str) -> bool:
    from difflib import SequenceMatcher
    import unicodedata

    if visual.casefold() == embedded.casefold():
        return False
    if any(unicodedata.name(character, "").startswith("MATHEMATICAL") for character in embedded):
        return False
    if min(len(visual), len(embedded)) < 4 or abs(len(visual) - len(embedded)) > 2:
        return False
    identifier = (
        any(character.isdigit() for character in visual)
        and any(character.isdigit() for character in embedded)
    )
    proper_name = visual[:1].isupper() and embedded[:1].isupper()
    if not (identifier or proper_name):
        return False
    if visual.isdigit() != embedded.isdigit():
        # A letter/digit confusion inside an otherwise stable identifier is safe.
        if not (visual.isalnum() and embedded.isalnum()):
            return False
    similarity = SequenceMatcher(None, visual.casefold(), embedded.casefold(), autojunk=False).ratio()
    return similarity >= 0.72


def _embedded_text_for_bbox(
    embedded: EmbeddedEvidence,
    bbox: tuple[float, float, float, float],
) -> str:
    matched: list[str] = []
    for block in embedded.blocks:
        candidate = block.get("bbox", [])
        if len(candidate) != 4:
            continue
        intersection = _intersection_area(bbox, candidate)
        if intersection / max(1.0, min(_bbox_area(bbox), _bbox_area(tuple(candidate)))) >= 0.35:
            matched.append(block.get("text", ""))
    return "\n".join(matched)


def _intersection_area(a, b) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, right - left) * max(0.0, bottom - top)


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


def _apply_links_to_blocks(
    blocks: list[Block],
    links,
    *,
    page_number: int | None = None,
) -> None:
    for link in links:
        label = " ".join(link.text.split())
        if not label or not link.target:
            continue
        # PDF GoTo annotations identify a destination page, not the exact
        # target block. Turning them into heading anchors can silently link a
        # theorem or equation reference to an unrelated heading on that page.
        if not link.external:
            continue
        if page_number is not None and link.target == f"#page-{page_number}":
            continue
        if any(f"]({link.target})" in block.markdown for block in blocks):
            continue
        eligible = [
            block
            for block in blocks
            if block.kind not in {"table", "figure"}
        ]
        if link.bbox:
            eligible = [
                block
                for block in eligible
                if block.bbox and _bbox_coverage(link.bbox, block.bbox) >= 0.35
            ]
            eligible.sort(
                key=lambda block: _bbox_coverage(link.bbox, block.bbox),
                reverse=True,
            )
        candidates = [
            block for block in eligible if _link_label_pattern(label).search(block.markdown)
        ]
        for block in candidates:
            updated, count = _replace_plain_text_once(block.markdown, label, link.target)
            if count:
                block.markdown = updated
                block.metadata.setdefault("links", []).append(
                    {"target": link.target, "source": "embedded_link_geometry"}
                )
                break
        else:
            if link.target.casefold().startswith("https://doi.org/"):
                for block in eligible:
                    updated = _replace_doi_tail(block.markdown, link.target)
                    if updated == block.markdown:
                        continue
                    block.markdown = updated
                    block.metadata.setdefault("links", []).append(
                        {"target": link.target, "source": "embedded_doi_target_geometry"}
                    )
                    break


def _replace_doi_tail(markdown: str, target: str) -> str:
    doi = target[len("https://doi.org/") :].strip().rstrip(".")
    if not doi.startswith("10."):
        return markdown
    pattern = re.compile(r"(?is)(\bdoi\s*:\s*)(?:10\..*)$")
    match = pattern.search(markdown)
    if not match:
        return markdown
    suffix = "." if markdown.rstrip().endswith(".") else ""
    replacement = f"{match.group(1)}[{doi}]({target}){suffix}"
    return markdown[: match.start()] + replacement


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
    embedded_title = _embedded_document_title(document)
    if embedded_title:
        return embedded_title
    metadata_title = str(document.metadata.get("title") or "").strip()
    if metadata_title and metadata_title.casefold() not in {"untitled", "none"}:
        return metadata_title
    return source.stem.replace("-", " ").replace("_", " ").strip().title()


def _embedded_document_title(document) -> str:
    """Use conspicuously large first-page PDF text as title evidence."""
    from statistics import median

    if not document.pages:
        return ""
    candidates: list[tuple[float, float, float, str]] = []
    sizes: list[float] = []
    for block in document.pages[0].embedded.blocks:
        block_sizes: list[float] = []
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                size = span.get("size")
                if isinstance(size, (int, float)) and size > 0:
                    block_sizes.append(float(size))
                    sizes.append(float(size))
        bbox = block.get("bbox", [])
        text = " ".join(str(block.get("text", "")).replace("\u00ad", "").split())
        if block_sizes and len(bbox) == 4 and bbox[1] <= 350 and len(text) >= 12:
            candidates.append((max(block_sizes), float(bbox[1]), float(bbox[3]), text))
    if not candidates or not sizes:
        return ""
    normal_size = median(sizes)
    candidates.sort(key=lambda item: (item[1], item[2]))
    joined: list[tuple[float, float, float, str]] = []
    for size, top, bottom, text in candidates:
        if (
            joined
            and abs(size - joined[-1][0]) <= max(0.5, size * 0.05)
            and 0 <= top - joined[-1][2] <= max(35.0, size * 2.2)
        ):
            previous_size, previous_top, _, previous_text = joined[-1]
            joined[-1] = (
                max(previous_size, size),
                previous_top,
                bottom,
                f"{previous_text} {text}",
            )
        else:
            joined.append((size, top, bottom, text))
    size, _, _, text = max(joined, key=lambda item: (item[0], -item[1]))
    return text if size >= normal_size * 1.25 else ""


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


def _page_checkpoints_complete(bundle: Path, metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    count = metadata.get("requested_page_count", metadata.get("page_count"))
    return bool(
        isinstance(count, int)
        and count >= 0
        and all(
            (bundle / "pages" / f"page-{number:04d}.json").is_file()
            for number in range(1, count + 1)
        )
    )


def _write_progress(
    bundle: Path,
    *,
    source: Path,
    fingerprint: dict[str, Any],
    completed_pages: set[int],
    status: str,
    errors: list[str] | None = None,
) -> None:
    atomic_json(
        bundle / "progress.json",
        {
            "schema_version": SCHEMA_VERSION,
            "source": str(source),
            "ocr_fingerprint": fingerprint["ocr"],
            "assembly_fingerprint": fingerprint["assembly"],
            "completed_pages": sorted(completed_pages),
            "status": status,
            "errors": errors or [],
            "updated_at": time.time(),
        },
    )


def _page_from_dict(value: dict[str, Any]) -> PageResult:
    embedded_value = value.get("embedded", {})
    embedded = EmbeddedEvidence(
        text=embedded_value.get("text", ""),
        blocks=embedded_value.get("blocks", []),
        links=[
            Link(
                text=link.get("text", ""),
                target=link.get("target", ""),
                bbox=tuple(link["bbox"]) if link.get("bbox") is not None else None,
                external=link.get("external", True),
            )
            for link in embedded_value.get("links", [])
        ],
        extractor=embedded_value.get("extractor"),
    )
    blocks = [_block_from_dict(block) for block in value.get("blocks", [])]
    return PageResult(
        number=value["number"],
        image=value.get("image", ""),
        visual_markdown=value.get("visual_markdown", ""),
        blocks=blocks,
        embedded=embedded,
        comparison=Comparison(**value.get("comparison", {})),
        warnings=value.get("warnings", []),
        generation=value.get("generation", {}),
        source_assets=value.get("source_assets", []),
        raw_ocr=value.get("raw_ocr", ""),
        visual=value.get("visual", {}),
        recovery=value.get("recovery", []),
    )


def _block_from_dict(value: dict[str, Any]) -> Block:
    return Block(
        kind=value.get("kind", "paragraph"),
        markdown=value.get("markdown", ""),
        bbox=tuple(value["bbox"]) if value.get("bbox") is not None else None,
        confidence=value.get("confidence"),
        asset_id=value.get("asset_id"),
        source_pages=list(value.get("source_pages", [])),
        provenance=deepcopy(value.get("provenance", [])),
        metadata=deepcopy(value.get("metadata", {})),
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
