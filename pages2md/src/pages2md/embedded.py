from __future__ import annotations

import re
import unicodedata
import math
from dataclasses import dataclass
from difflib import SequenceMatcher

from .model import EmbeddedEvidence


@dataclass(frozen=True)
class EmbeddedTrust:
    state: str
    score: float
    reasons: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.state != "untrusted"

    @property
    def geometric(self) -> bool:
        return self.state == "trusted"


def assess_embedded(
    embedded: EmbeddedEvidence | None,
    visual: str = "",
) -> EmbeddedTrust:
    """Classify an embedded text layer before it is allowed to affect OCR.

    The score deliberately uses only document-independent signals: printable
    text, valid character boxes, non-repetition, and agreement with the visual
    OCR. A partial layer may corroborate local edits but may not create content.
    """
    if embedded is None or embedded.extractor == "ignored":
        return EmbeddedTrust("untrusted", 0.0, ("disabled",))
    text = unicodedata.normalize("NFKC", "".join(
        character for character in (embedded.text or "")
        if not _is_variation_selector(character)
    ))
    if not text.strip():
        return EmbeddedTrust("untrusted", 0.0, ("empty",))

    score = 1.0
    reasons: list[str] = []
    printable = sum(character.isprintable() or character in "\n\t" for character in text)
    if printable / max(1, len(text)) < 0.98:
        score -= 0.45
        reasons.append("nonprinting_characters")
    if text.count("\ufffd") / max(1, len(text)) > 0.002:
        score -= 0.55
        reasons.append("replacement_characters")
    if _severe_repetition(text):
        score -= 0.65
        reasons.append("repetition")

    # Variation selectors modify a preceding glyph and have no independent
    # ink box. Do not mistake their legitimate zero advance for broken text.
    # A cluster containing a visible base still needs valid geometry; ordinary
    # combining marks are not exempted either.
    characters = [character for character in iter_embedded_characters(embedded)
                  if any(not c.isspace() and not _is_variation_selector(c)
                         for c in character["text"])]
    if characters:
        valid = sum(_valid_bbox(character["bbox"]) for character in characters)
        if valid / len(characters) < 0.98:
            score -= 0.5
            reasons.append("invalid_geometry")
    else:
        score -= 0.25
        reasons.append("no_character_geometry")

    visual_norm = _plain_normalize(visual)
    embedded_norm = _plain_normalize(text)
    if len(visual_norm) >= 40 and len(embedded_norm) >= 40:
        similarity = SequenceMatcher(
            None, visual_norm.casefold(), embedded_norm.casefold(), autojunk=False
        ).ratio()
        visual_tokens = set(re.findall(r"\w+", visual_norm.casefold()))
        embedded_tokens = set(re.findall(r"\w+", embedded_norm.casefold()))
        coverage = len(visual_tokens & embedded_tokens) / max(1, len(visual_tokens))
        agreement = max(similarity, coverage)
        if agreement < 0.2 or (coverage < 0.1 and similarity < 0.35):
            score -= 0.75
            reasons.append("visual_disagreement")
        elif agreement < 0.45:
            score -= 0.3
            reasons.append("weak_visual_agreement")
        length_ratio = len(embedded_norm) / max(1, len(visual_norm))
        if not 0.35 <= length_ratio <= 2.5:
            score -= 0.25
            reasons.append("length_mismatch")

    score = round(max(0.0, min(1.0, score)), 6)
    state = "trusted" if score >= 0.7 else "partial" if score >= 0.4 else "untrusted"
    return EmbeddedTrust(state, score, tuple(reasons))


def iter_embedded_characters(embedded: EmbeddedEvidence | None):
    if embedded is None:
        return
    for block_index, block in enumerate(embedded.blocks):
        for line_index, line in enumerate(block.get("lines", [])):
            for span_index, span in enumerate(line.get("spans", [])):
                for character_index, character in enumerate(span.get("chars", [])):
                    value = character.get("text", "")
                    bbox = character.get("bbox", [])
                    if not value or len(bbox) != 4:
                        continue
                    yield {
                        "text": value,
                        "bbox": tuple(float(item) for item in bbox),
                        "font": span.get("font", ""),
                        "font_xrefs": span.get("font_xrefs", []),
                        "size": span.get("size"),
                        "flags": span.get("flags"),
                        "origin": character.get("origin"),
                        "em": span.get("em"),
                        "direction": line.get("direction", [1, 0]),
                        "order": (block_index, line_index, span_index, character_index),
                    }


def embedded_text_for_bbox(
    embedded: EmbeddedEvidence | None,
    bbox: tuple[float, float, float, float] | None,
    *,
    minimum_coverage: float = 0.35,
) -> str:
    if embedded is None or bbox is None:
        return ""
    selected: list[str] = []
    for block in embedded.blocks:
        block_box = block.get("bbox", [])
        if len(block_box) != 4 or bbox_coverage(block_box, bbox) < minimum_coverage:
            continue
        selected.append(str(block.get("text", "")))
    return "\n".join(value for value in selected if value).strip()


def embedded_characters_for_bbox(
    embedded: EmbeddedEvidence | None,
    bbox: tuple[float, float, float, float] | None,
    *,
    minimum_coverage: float = 0.45,
) -> list[dict[str, object]]:
    if bbox is None:
        return []
    return [
        character
        for character in iter_embedded_characters(embedded)
        if bbox_coverage(character["bbox"], bbox) >= minimum_coverage
    ]


def bbox_coverage(subject, container) -> float:
    left, top = max(subject[0], container[0]), max(subject[1], container[1])
    right, bottom = min(subject[2], container[2]), min(subject[3], container[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area = max(1.0, (subject[2] - subject[0]) * (subject[3] - subject[1]))
    return intersection / area


def bbox_iou(left_box, right_box) -> float:
    left, top = max(left_box[0], right_box[0]), max(left_box[1], right_box[1])
    right, bottom = min(left_box[2], right_box[2]), min(left_box[3], right_box[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = max(
        1.0,
        (left_box[2] - left_box[0]) * (left_box[3] - left_box[1])
        + (right_box[2] - right_box[0]) * (right_box[3] - right_box[1])
        - intersection,
    )
    return intersection / union


def _is_variation_selector(character: str) -> bool:
    name = unicodedata.name(character, "")
    return name.startswith(("VARIATION SELECTOR-", "MONGOLIAN FREE VARIATION SELECTOR"))


def _valid_bbox(bbox) -> bool:
    return bool(
        len(bbox) == 4
        and 0 <= bbox[0] < bbox[2] <= 1000
        and 0 <= bbox[1] < bbox[3] <= 1000
    )


def _plain_normalize(value: str) -> str:
    value = re.sub(r"\\[A-Za-z]+", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _severe_repetition(value: str) -> bool:
    words = value.split()
    for size in range(2, min(20, len(words) // 4 + 1)):
        for start in range(min(2000, len(words) - size * 4 + 1)):
            phrase = words[start : start + size]
            minimum_repeats = max(4, math.ceil(80 / max(1, len(" ".join(phrase)))))
            if start + minimum_repeats * size > len(words):
                continue
            if all(
                words[start + repeat * size : start + (repeat + 1) * size] == phrase
                for repeat in range(1, minimum_repeats)
            ):
                return True
    return False
