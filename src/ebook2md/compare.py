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
    if not embedded_norm:
        return Comparison(warnings=["embedded_text_absent"])
    similarity = SequenceMatcher(None, visual_norm, embedded_norm, autojunk=False).ratio()
    visual_tokens = set(visual_norm.lower().split())
    embedded_tokens = set(embedded_norm.lower().split())
    coverage = len(visual_tokens & embedded_tokens) / max(1, len(visual_tokens))
    ratio = len(embedded_norm) / max(1, len(visual_norm))
    math_differs = (set(visual_norm) & MATH) != (set(embedded_norm) & MATH)
    warnings: list[str] = []
    if similarity < 0.90:
        warnings.append("embedded_text_low_similarity")
    if not 0.8 <= ratio <= 1.2:
        warnings.append("embedded_text_length_mismatch")
    if math_differs:
        warnings.append("embedded_text_math_mismatch")
    if "\ufffd" in visual or "\ufffd" in embedded:
        warnings.append("replacement_character_present")
    return Comparison(
        character_similarity=round(similarity, 6),
        token_coverage=round(coverage, 6),
        length_ratio=round(ratio, 6),
        math_symbol_differs=math_differs,
        warnings=warnings,
    )

