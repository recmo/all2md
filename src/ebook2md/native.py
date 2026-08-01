from __future__ import annotations

import hashlib
import math
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
    supplied_generation = dict(generation or {})
    confidence_spans = supplied_generation.pop("_confidence_spans", [])
    observation = OcrObservation(
        id=observation_id(mode, raw, source_pages),
        mode=mode,
        raw=raw,
        source_pages=list(source_pages),
        generation=supplied_generation,
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
    _apply_block_confidence(observation, confidence_spans)
    observation.warnings = validate_observation(observation)
    return observation


def _apply_block_confidence(observation: OcrObservation, spans: list[dict[str, Any]]) -> None:
    if not spans:
        return
    cursor = 0
    for block in observation.blocks:
        start = observation.raw.find(block.markdown, cursor)
        if start < 0:
            continue
        end = start + len(block.markdown)
        cursor = end
        overlapping = [
            span
            for span in spans
            if int(span.get("end", 0)) > start and int(span.get("start", 0)) < end
        ]
        logprobs = [float(value) for span in overlapping for value in span.get("logprobs", [])]
        if not logprobs:
            continue
        probabilities = sorted(math.exp(max(-100.0, min(0.0, value))) for value in logprobs)
        fifth = probabilities[max(0, math.ceil(len(probabilities) * 0.05) - 1)]
        block.confidence = round(math.exp(sum(logprobs) / len(logprobs)), 6)
        block.metadata["token_confidence"] = {
            "token_count": len(probabilities),
            "p05_probability": round(fifth, 6),
            "minimum_probability": round(probabilities[0], 6),
            "below_half_fraction": round(
                sum(value < 0.5 for value in probabilities) / len(probabilities), 6
            ),
        }
        uncertain = []
        for span in overlapping:
            values = [float(value) for value in span.get("logprobs", [])]
            if not values:
                continue
            local_confidence = math.exp(sum(values) / len(values))
            if local_confidence >= 0.7:
                continue
            local_start = max(0, int(span.get("start", 0)) - start)
            local_end = min(len(block.markdown), int(span.get("end", 0)) - start)
            if local_start >= local_end:
                continue
            uncertain.append({
                "start": local_start,
                "end": local_end,
                "text": block.markdown[local_start:local_end],
                "confidence": round(local_confidence, 6),
            })
        if uncertain:
            block.metadata["uncertain_spans"] = _merge_uncertain_spans(uncertain, block.markdown)


def _merge_uncertain_spans(spans: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for span in sorted(spans, key=lambda item: (item["start"], item["end"])):
        if merged and span["start"] <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
            merged[-1]["confidence"] = min(merged[-1]["confidence"], span["confidence"])
            merged[-1]["text"] = text[merged[-1]["start"] : merged[-1]["end"]]
        else:
            merged.append(dict(span))
    return merged


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
    *,
    embedded_text: str = "",
) -> tuple[list[Block], list[dict[str, Any]], list[str]]:
    """Keep multi-page structure and apply only confidently aligned Gundam spans."""
    provenance: list[dict[str, Any]] = []
    warnings: list[str] = list(primary.warnings)
    canonical = [_copy_block(block) for block in primary.blocks]
    primary_bad = bool(
        set(primary.warnings)
        & {"visual_empty_output", "visual_malformed_grounding", "visual_text_repetition", "visual_truncated"}
    )
    page_detail = [
        recovery
        for recovery in recoveries
        if isinstance(recovery.generation.get("target_block_indices"), list)
    ]
    if page_detail and not primary_bad:
        return _reconcile_page_detail(
            primary,
            page_detail[0],
            canonical,
            warnings,
            embedded_text=embedded_text,
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
    for block in canonical:
        if _low_confidence(block) and not block.metadata.get("review_required"):
            _mark_unresolved(block, detail=None)
            warnings.append("visual_low_ocr_confidence")
    return canonical, provenance, sorted(set(warnings))


def _reconcile_page_detail(
    primary: OcrObservation,
    recovery: OcrObservation,
    canonical: list[Block],
    warnings: list[str],
    *,
    embedded_text: str,
) -> tuple[list[Block], list[dict[str, Any]], list[str]]:
    provenance: list[dict[str, Any]] = []
    warnings.extend(recovery.warnings)
    targeted = {
        int(index)
        for index in recovery.generation.get("target_block_indices", [])
        if isinstance(index, int) or str(index).isdigit()
    }
    for index, block in enumerate(canonical):
        compatible = [candidate for candidate in recovery.blocks if _compatible(block, candidate)]
        candidate = max(compatible, key=lambda item: _alignment_score(block, item), default=None)
        if candidate is None or _alignment_score(block, candidate) < 0.55:
            if index in targeted:
                _mark_unresolved(block, detail=None)
                warnings.append("visual_target_alignment_failed")
            continue
        block = canonical[index]
        if _evidence_normalize(candidate.markdown) == _evidence_normalize(block.markdown):
            block.metadata.pop("uncertain_spans", None)
            block.metadata["targeted_detail_agreed"] = recovery.id
            continue

        base_local = _local_confidence(block)
        candidate_local = _local_confidence(candidate)
        base_evidence = _embedded_support(block.markdown, embedded_text)
        candidate_evidence = _embedded_support(candidate.markdown, embedded_text)
        evidence_delta = candidate_evidence - base_evidence
        detail_preferred = bool(
            _structural_penalty(candidate.markdown) <= _structural_penalty(block.markdown)
            and (
                evidence_delta > 0.0005
                or candidate_local >= base_local + 0.25
                or (
                    candidate.confidence is not None
                    and block.confidence is not None
                    and candidate.confidence >= block.confidence + 0.0005
                )
                or (abs(evidence_delta) <= 0.0005 and candidate_local >= base_local + 0.1)
            )
        )
        base_preferred = bool(
            evidence_delta < -0.0005
            or (
                abs(evidence_delta) <= 0.0005
                and base_local >= candidate_local + 0.1
            )
        )
        if detail_preferred:
            replacement = _copy_block(candidate)
            replacement.source_pages = list(block.source_pages)
            replacement.bbox = block.bbox
            replacement.metadata["targeted_from_spans"] = block.metadata.get("uncertain_spans", [])
            replacement.metadata.pop("uncertain_spans", None)
            replacement.provenance = [
                *block.provenance,
                {
                    "observation": recovery.id,
                    "action": "selected_targeted_detail",
                    "base_local_confidence": round(base_local, 6),
                    "detail_local_confidence": round(candidate_local, 6),
                    "embedded_support_delta": round(evidence_delta, 6),
                },
            ]
            canonical[index] = replacement
            provenance.append({
                "block": index,
                "primary_observation": primary.id,
                "recovery_observation": recovery.id,
                "action": "selected_targeted_detail",
                "base_local_confidence": round(base_local, 6),
                "detail_local_confidence": round(candidate_local, 6),
                "embedded_support_delta": round(evidence_delta, 6),
            })
        elif base_preferred:
            block.metadata.pop("uncertain_spans", None)
            block.metadata["targeted_detail_rejected"] = recovery.id
        elif index in targeted:
            # Keep Base on a tie because it preserves the multi-page reading order.
            _mark_unresolved(block, detail=candidate)
            warnings.append("visual_targeted_ocr_unresolved")
    for index, block in enumerate(canonical):
        if _low_confidence(block) and index not in targeted:
            _mark_unresolved(block, detail=None)
            warnings.append("visual_low_ocr_confidence")
    return canonical, provenance, sorted(set(warnings))


def _local_confidence(block: Block) -> float:
    spans = block.metadata.get("uncertain_spans", [])
    if spans:
        return min(float(span.get("confidence", 0.0)) for span in spans)
    return float(block.confidence if block.confidence is not None else 1.0)


def _mark_unresolved(block: Block, detail: Block | None) -> None:
    base_excerpt, detail_excerpt = _changed_excerpts(
        block.markdown,
        detail.markdown if detail is not None else "",
    )
    block.metadata.update({
        "review_required": True,
        "review_reason": "targeted_ocr_unresolved",
        "review_confidence": _local_confidence(block),
        "review_base": base_excerpt,
        "review_detail": detail_excerpt or None,
    })


def _changed_excerpts(base: str, detail: str, *, context: int = 24) -> tuple[str, str]:
    if not detail:
        spans = []
        return base[: context * 2].strip(), ""
    matcher = SequenceMatcher(None, base, detail, autojunk=False)
    opcode = next((item for item in matcher.get_opcodes() if item[0] != "equal"), None)
    if opcode is None:
        return base[: context * 2].strip(), detail[: context * 2].strip()
    _, left_start, left_end, right_start, right_end = opcode
    return (
        base[max(0, left_start - context) : min(len(base), left_end + context)].strip(),
        detail[max(0, right_start - context) : min(len(detail), right_end + context)].strip(),
    )


def _low_confidence(block: Block) -> bool:
    return bool(block.metadata.get("uncertain_spans"))


def _embedded_support(value: str, embedded: str) -> float:
    if not embedded:
        return 0.0
    visual = _evidence_normalize(value)
    evidence = _evidence_normalize(embedded)
    if not visual or not evidence:
        return 0.0
    matcher = SequenceMatcher(None, visual, evidence, autojunk=False)
    return 2 * sum(block.size for block in matcher.get_matching_blocks()) / (
        len(visual) + len(evidence)
    )


def _evidence_normalize(value: str) -> str:
    value = re.sub(
        r"\\(?:mathbb|mathbf|mathrm|operatorname|text)\s*\{([^{}]*)\}",
        r"\1",
        value,
    )
    value = value.replace(r"\{", "{").replace(r"\}", "}")
    value = value.replace(r"\(", " ").replace(r"\)", " ")
    value = value.replace(r"\[", " ").replace(r"\]", " ")
    value = value.replace("**", "").replace("□", " ").replace("•", " ")
    value = value.replace(r"\colon", ":")
    value = re.sub(r"\\([A-Za-z]+)", r"\1", value)
    value = re.sub(r"_\{([^{}]+)\}", r"_\1", value)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return normalize(value).casefold()


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
