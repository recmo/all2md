from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .model import Comparison

MATH = set("∑∏∫√∞≈≠≤≥±×÷∂∇∈∉⊂⊆∪∩∀∃λμσπτφψω")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def compare_text(visual: str, embedded: str) -> Comparison:
    visual_norm = normalize(visual)
    embedded_norm = normalize(embedded)
    visual_repeats = _has_severe_repetition(visual_norm)
    if not embedded_norm:
        warnings = ["embedded_text_absent"]
        if visual_repeats:
            warnings.append("visual_text_repetition")
        return Comparison(warnings=warnings)
    similarity = SequenceMatcher(None, visual_norm, embedded_norm, autojunk=False).ratio()
    visual_tokens = set(visual_norm.lower().split())
    embedded_tokens = set(embedded_norm.lower().split())
    coverage = len(visual_tokens & embedded_tokens) / max(1, len(visual_tokens))
    ratio = len(embedded_norm) / max(1, len(visual_norm))
    math_differs = (set(visual_norm) & MATH) != (set(embedded_norm) & MATH)
    reading_order_differs = coverage >= 0.7 and similarity < 0.65
    warnings: list[str] = []
    if similarity < 0.90:
        warnings.append("embedded_text_low_similarity")
    if not 0.8 <= ratio <= 1.2:
        warnings.append("embedded_text_length_mismatch")
    if math_differs:
        warnings.append("embedded_text_math_mismatch")
    if reading_order_differs:
        warnings.append("embedded_text_reading_order_mismatch")
    if "\ufffd" in visual or "\ufffd" in embedded:
        warnings.append("replacement_character_present")
    if visual_repeats:
        warnings.append("visual_text_repetition")
    if _has_severe_repetition(embedded_norm):
        warnings.append("embedded_text_repetition")
    return Comparison(
        character_similarity=round(similarity, 6),
        token_coverage=round(coverage, 6),
        length_ratio=round(ratio, 6),
        reading_order_differs=reading_order_differs,
        math_symbol_differs=math_differs,
        warnings=warnings,
    )


def _has_severe_repetition(text: str) -> bool:
    words = text.split()
    for size in range(8, min(36, len(words) // 3 + 1)):
        for start in range(0, len(words) - size * 3 + 1):
            phrase = words[start : start + size]
            if phrase == words[start + size : start + 2 * size] == words[start + 2 * size : start + 3 * size]:
                return True
    return False
