from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any

from bs4 import BeautifulSoup

from .compare import compare_text, normalize
from .model import Block, OcrObservation

PAGE_TOKEN = re.compile(r"\s*<PAGE>\s*")
DET_TOKEN = re.compile(r"<\|det\|>(.*?)<\|/det\|>", re.DOTALL)
REF_BEFORE = re.compile(r"<\|ref\|>(.*?)<\|/ref\|>\s*$", re.DOTALL)
RAW_GROUNDING = re.compile(r"<\|/?(?:ref|det)\|>")
HTML_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
COORDINATE = re.compile(r"-?\d+(?:\.\d+)?")
HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")

KIND_ALIASES = {
    "page_title": "heading",
    "section_header": "heading",
    "header": "header",
    "title": "heading",
    "text": "paragraph",
    "body": "paragraph",
    "display_formula": "formula",
    "equation": "formula",
    "image": "figure",
    "illustration": "figure",
    "graphic": "figure",
    "diagram": "figure",
    "chart": "figure",
    "photo": "figure",
}


def observation_id(mode: str, raw: str, pages: list[int]) -> str:
    digest = hashlib.sha256()
    digest.update(mode.encode())
    digest.update(",".join(map(str, pages)).encode())
    digest.update(raw.encode())
    return f"{mode}-{digest.hexdigest()[:16]}"


def parse_native_observation(
    raw: str,
    *,
    mode: str,
    source_pages: list[int],
    generation: dict[str, Any] | None = None,
) -> OcrObservation:
    """Parse Unlimited-OCR's native hybrid stream without rewriting its raw text."""
    observation = OcrObservation(
        id=observation_id(mode, raw, source_pages),
        mode=mode,
        raw=raw,
        source_pages=list(source_pages),
        generation=dict(generation or {}),
    )
    segments = PAGE_TOKEN.split(raw)
    if segments and not segments[0].strip():
        segments = segments[1:]
    if len(segments) == len(source_pages):
        segment_pages = [[page] for page in source_pages]
    else:
        segment_pages = [list(source_pages) for _ in segments]
    for segment_index, segment in enumerate(segments):
        pages = segment_pages[min(segment_index, len(segment_pages) - 1)] if segment_pages else source_pages
        observation.blocks.extend(_parse_segment(segment, pages, observation.id))
    observation.warnings = validate_observation(observation)
    return observation


def _parse_segment(segment: str, pages: list[int], observation: str) -> list[Block]:
    matches = list(DET_TOKEN.finditer(segment))
    if not matches:
        return _parse_ungrounded(segment, pages, observation)
    blocks: list[Block] = []
    cursor = 0
    for index, match in enumerate(matches):
        prefix = segment[cursor : match.start()]
        ref_match = REF_BEFORE.search(prefix)
        label = ref_match.group(1).strip() if ref_match else ""
        ungrounded = prefix[: ref_match.start()] if ref_match else prefix
        blocks.extend(_parse_ungrounded(ungrounded, pages, observation))
        payload = match.group(1).strip()
        if not label and "[" in payload:
            label = payload.split("[", 1)[0].strip()
        bbox = _parse_bbox(payload)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(segment)
        content_region = segment[match.end() : end]
        next_ref = REF_BEFORE.search(content_region)
        content = content_region[: next_ref.start()] if next_ref else content_region
        cursor = end - (len(content_region) - next_ref.start()) if next_ref else end
        kind = _kind(label, content)
        blocks.append(
            Block(
                kind=kind,
                markdown=content.strip(),
                bbox=bbox,
                source_pages=list(pages),
                provenance=[{"observation": observation, "action": "native"}],
                metadata={"native_label": label or None},
            )
        )
    return [block for block in blocks if block.markdown or block.kind in {"figure", "formula"}]


def _parse_ungrounded(content: str, pages: list[int], observation: str) -> list[Block]:
    content = content.strip()
    if not content:
        return []
    blocks: list[Block] = []
    cursor = 0
    for table in HTML_TABLE.finditer(content):
        blocks.extend(_plain_blocks(content[cursor : table.start()], pages, observation))
        blocks.append(
            Block(
                kind="table",
                markdown=table.group(0).strip(),
                source_pages=list(pages),
                provenance=[{"observation": observation, "action": "native"}],
            )
        )
        cursor = table.end()
    blocks.extend(_plain_blocks(content[cursor:], pages, observation))
    return blocks


def _plain_blocks(content: str, pages: list[int], observation: str) -> list[Block]:
    result: list[Block] = []
    for piece in re.split(r"\n\s*\n", content.strip()):
        piece = piece.strip()
        if not piece:
            continue
        first = piece.splitlines()[0]
        if HEADING.match(first):
            kind = "heading"
        elif all(LIST_ITEM.match(line) for line in piece.splitlines() if line.strip()):
            kind = "list"
        elif _looks_like_formula(piece):
            kind = "formula"
        else:
            kind = "paragraph"
        result.append(
            Block(
                kind=kind,
                markdown=piece,
                source_pages=list(pages),
                provenance=[{"observation": observation, "action": "native"}],
                metadata={"native_ungrounded": True},
            )
        )
    return result


def _parse_bbox(payload: str):
    bracket = payload[payload.find("[") :] if "[" in payload else payload
    values = [float(value) for value in COORDINATE.findall(bracket)]
    if len(values) < 4 or len(values) % 4:
        return None
    boxes = [values[index : index + 4] for index in range(0, len(values), 4)]
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _kind(label: str, content: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")
    if "<table" in content.casefold() or normalized == "table":
        return "table"
    return KIND_ALIASES.get(normalized, normalized or "paragraph")


def validate_observation(observation: OcrObservation) -> list[str]:
    warnings: list[str] = []
    raw = observation.raw
    if not raw.strip():
        warnings.append("visual_empty_output")
    if raw.count("<|det|>") != raw.count("<|/det|>") or raw.count("<|ref|>") != raw.count("<|/ref|>"):
        warnings.append("visual_malformed_grounding")
    if RAW_GROUNDING.search(_without_complete_grounding(raw)):
        warnings.append("visual_malformed_grounding")
    for block in observation.blocks:
        if block.bbox is None:
            continue
        left, top, right, bottom = block.bbox
        if left < 0 or top < 0 or right > 1000 or bottom > 1000 or left >= right or top >= bottom:
            warnings.append("visual_implausible_coordinates")
            break
    if "visual_text_repetition" in compare_text(_observation_text(observation), "").warnings:
        warnings.append("visual_text_repetition")
    if observation.generation.get("finish_reason") in {"length", "max_tokens"}:
        warnings.append("visual_truncated")
    if len(observation.source_pages) > 1:
        segment_count = len([part for part in PAGE_TOKEN.split(raw) if part.strip()])
        if segment_count != len(observation.source_pages):
            warnings.append("visual_page_transition_mismatch")
    if _malformed_table(raw):
        warnings.append("visual_malformed_table")
    return sorted(set(warnings))


def reconcile_observations(
    primary: OcrObservation,
    recoveries: list[OcrObservation],
) -> tuple[list[Block], list[dict[str, Any]], list[str]]:
    """Keep multi-page structure and apply only confidently aligned Gundam spans."""
    provenance: list[dict[str, Any]] = []
    warnings: list[str] = list(primary.warnings)
    canonical = []
    for block in primary.blocks:
        if _suspicious_ungrounded(block):
            warnings.append("visual_suspicious_ungrounded_preamble")
        else:
            canonical.append(_copy_block(block))
    primary_bad = bool(
        set(primary.warnings)
        & {"visual_empty_output", "visual_malformed_grounding", "visual_text_repetition", "visual_truncated"}
    )
    if len(recoveries) >= 2 and not primary_bad:
        return _reconcile_consensus(primary, recoveries, canonical, warnings)
    for recovery in recoveries:
        warnings.extend(recovery.warnings)
        if _should_replace_corrupt_local_page(primary, recovery):
            recovery_text = normalize(_observation_text(recovery)).casefold()
            retained_headings = [
                block
                for block in canonical
                if block.kind == "heading"
                and normalize(block.markdown).casefold() not in recovery_text
                and not any(
                    block.bbox
                    and candidate.bbox
                    and (_iou(block.bbox, candidate.bbox) >= 0.2 or _coverage(block.bbox, candidate.bbox) >= 0.5)
                    for candidate in recovery.blocks
                )
            ]
            canonical = retained_headings
            for candidate in recovery.blocks:
                if _suspicious_ungrounded(candidate):
                    warnings.append("visual_suspicious_ungrounded_preamble")
                    continue
                adopted = _copy_block(candidate)
                adopted.provenance.append(
                    {"observation": primary.id, "action": "replaced_corrupt_page_local_content"}
                )
                canonical.append(adopted)
                provenance.append(
                    {
                        "block": len(canonical) - 1,
                        "primary_observation": primary.id,
                        "recovery_observation": recovery.id,
                        "action": "replaced_corrupt_page_local_content",
                        "score": 1.0,
                    }
                )
            continue
        used: set[int] = set()
        for index, block in enumerate(canonical):
            if block.kind == "table":
                continue
            candidates = [
                (candidate_index, candidate)
                for candidate_index, candidate in enumerate(recovery.blocks)
                if candidate_index not in used and _compatible(block, candidate)
            ]
            if not candidates:
                continue
            candidate_index, candidate = max(candidates, key=lambda item: _alignment_score(block, item[1]))
            score = _alignment_score(block, candidate)
            old_text, new_text = normalize(block.markdown), normalize(candidate.markdown)
            improves = len(new_text) > len(old_text) and (primary_bad or len(old_text) < 24)
            if score >= 0.65 and improves:
                block.markdown = candidate.markdown
                block.provenance.append(
                    {"observation": recovery.id, "action": "replaced_local_content", "score": round(score, 4)}
                )
                provenance.append(
                    {
                        "block": index,
                        "primary_observation": primary.id,
                        "recovery_observation": recovery.id,
                        "action": "replaced_local_content",
                        "score": round(score, 4),
                    }
                )
                used.add(candidate_index)
        if primary_bad and not canonical:
            for candidate in recovery.blocks:
                if _suspicious_ungrounded(candidate):
                    warnings.append("visual_suspicious_ungrounded_preamble")
                    continue
                adopted = _copy_block(candidate)
                adopted.provenance.append({"observation": primary.id, "action": "filled_empty_primary"})
                canonical.append(adopted)
                provenance.append(
                    {
                        "block": len(canonical) - 1,
                        "primary_observation": primary.id,
                        "recovery_observation": recovery.id,
                        "action": "filled_empty_primary",
                        "score": 1.0,
                    }
                )
        elif any(index not in used for index in range(len(recovery.blocks))) and primary_bad:
            warnings.append("visual_reconciliation_uncertain")
    return canonical, provenance, sorted(set(warnings))


def _reconcile_consensus(
    primary: OcrObservation,
    candidates: list[OcrObservation],
    canonical: list[Block],
    warnings: list[str],
) -> tuple[list[Block], list[dict[str, Any]], list[str]]:
    provenance: list[dict[str, Any]] = []
    warnings.extend(warning for candidate in candidates for warning in candidate.warnings)
    for index, block in enumerate(canonical):
        variants: list[tuple[str, Block]] = [(primary.id, block)]
        for observation in candidates:
            compatible = [candidate for candidate in observation.blocks if _compatible(block, candidate)]
            if not compatible:
                continue
            candidate = max(compatible, key=lambda item: _alignment_score(block, item))
            if _alignment_score(block, candidate) >= 0.55:
                variants.append((observation.id, candidate))
        if len(variants) < 2:
            continue

        normalized = [normalize(candidate.markdown) for _, candidate in variants]
        similarities = [
            SequenceMatcher(None, left, right, autojunk=False).ratio()
            for left_index, left in enumerate(normalized)
            for right in normalized[left_index + 1 :]
        ]
        minimum_similarity = min(similarities, default=1.0)
        scores = []
        for candidate_index, (_, candidate) in enumerate(variants):
            peers = [
                SequenceMatcher(None, normalized[candidate_index], peer, autojunk=False).ratio()
                for peer_index, peer in enumerate(normalized)
                if peer_index != candidate_index
            ]
            consensus = sum(peers) / max(1, len(peers))
            scores.append(consensus - _structural_penalty(candidate.markdown))

        winner_index = max(range(len(variants)), key=lambda item: (scores[item], item == 0))
        winner_id, winner = variants[winner_index]
        if block.kind != "table" and winner_index:
            block.kind = winner.kind
            block.markdown = winner.markdown
            block.provenance.append(
                {
                    "observation": winner_id,
                    "action": "selected_ocr_consensus",
                    "score": round(scores[winner_index], 6),
                }
            )
            provenance.append(
                {
                    "block": index,
                    "primary_observation": primary.id,
                    "recovery_observation": winner_id,
                    "action": "selected_ocr_consensus",
                    "score": round(scores[winner_index], 6),
                }
            )
        if minimum_similarity < 0.985:
            warnings.append("visual_ocr_disagreement")
            block.metadata.update(
                {
                    "review_required": True,
                    "review_reason": "ocr_candidates_disagree",
                    "review_consensus": round(max(scores), 6),
                    "review_candidates": [identifier for identifier, _ in variants],
                }
            )
    return canonical, provenance, sorted(set(warnings))


def _structural_penalty(value: str) -> float:
    penalty = 0.0
    for opening, closing in ((r"\(", r"\)"), (r"\[", r"\]")):
        penalty += 0.08 * abs(value.count(opening) - value.count(closing))
    penalty += 0.08 * (value.count("$") % 2)
    penalty += 0.2 * value.count("�")
    return penalty


def _should_replace_corrupt_local_page(primary: OcrObservation, recovery: OcrObservation) -> bool:
    severe = bool(
        set(primary.warnings)
        & {
            "visual_empty_output",
            "visual_malformed_grounding",
            "visual_text_repetition",
            "visual_truncated",
        }
    )
    if not severe or set(recovery.warnings) & {"visual_empty_output", "visual_text_repetition", "visual_truncated"}:
        return False
    # Existing multi-page tables are structural authority and are never replaced.
    if any(block.kind == "table" for block in primary.blocks):
        return False
    primary_tokens = set(normalize(_observation_text(primary)).casefold().split())
    recovery_tokens = set(normalize(_observation_text(recovery)).casefold().split())
    overlap = len(primary_tokens & recovery_tokens) / max(1, min(len(primary_tokens), len(recovery_tokens)))
    return not primary.blocks or overlap >= 0.35


def _suspicious_ungrounded(block: Block) -> bool:
    return bool(
        block.metadata.get("native_ungrounded")
        and block.bbox is None
        and re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日", block.markdown.strip())
    )


def observation_dict(observation: OcrObservation, *, raw_path: str) -> dict[str, Any]:
    value = asdict(observation)
    value.pop("raw", None)
    value["raw_path"] = raw_path
    return value


def _compatible(left: Block, right: Block) -> bool:
    if left.kind != right.kind and {left.kind, right.kind} - {"paragraph", "heading"}:
        return False
    if left.bbox and right.bbox:
        return _iou(left.bbox, right.bbox) >= 0.2
    return bool(normalize(left.markdown) and normalize(right.markdown))


def _alignment_score(left: Block, right: Block) -> float:
    text = SequenceMatcher(None, normalize(left.markdown), normalize(right.markdown), autojunk=False).ratio()
    geometry = _iou(left.bbox, right.bbox) if left.bbox and right.bbox else 0.5
    return 0.75 * text + 0.25 * geometry


def _iou(a, b) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = max(1.0, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection)
    return intersection / union


def _coverage(subject, container) -> float:
    left, top = max(subject[0], container[0]), max(subject[1], container[1])
    right, bottom = min(subject[2], container[2]), min(subject[3], container[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area = max(1.0, (subject[2] - subject[0]) * (subject[3] - subject[1]))
    return intersection / area


def _copy_block(block: Block) -> Block:
    return Block(**asdict(block))


def _looks_like_formula(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(("$$", "\\[", "\\begin{")) or stripped.endswith(("$$", "\\]"))


def _without_complete_grounding(raw: str) -> str:
    raw = re.sub(r"<\|ref\|>.*?<\|/ref\|>", "", raw, flags=re.DOTALL)
    return re.sub(r"<\|det\|>.*?<\|/det\|>", "", raw, flags=re.DOTALL)


def _malformed_table(raw: str) -> bool:
    folded = raw.casefold()
    if folded.count("<table") != folded.count("</table>"):
        return True
    for match in re.finditer(r"<table\b.*?</table>", raw, re.IGNORECASE | re.DOTALL):
        table = BeautifulSoup(match.group(0), "html.parser").find("table")
        if table is None or not table.find("tr"):
            return True
    return False


def _observation_text(observation: OcrObservation) -> str:
    return "\n\n".join(block.markdown for block in observation.blocks if block.markdown)
