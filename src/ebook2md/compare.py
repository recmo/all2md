from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .model import Comparison

MATH = set("∑∏∫√∞≈≠≤≥±×÷∂∇∈∉⊂⊆∪∩∀∃λμσπτφψω")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
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
    disagreements = _token_disagreements(visual_norm, embedded_norm)
    warnings: list[str] = []
    if similarity < 0.90:
        warnings.append("embedded_text_low_similarity")
    if not 0.8 <= ratio <= 1.2:
        warnings.append("embedded_text_length_mismatch")
    if math_differs:
        warnings.append("embedded_text_math_mismatch")
    if reading_order_differs:
        warnings.append("embedded_text_reading_order_mismatch")
    if disagreements:
        warnings.append("embedded_text_token_disagreement")
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
        disagreements=disagreements,
    )


def _has_severe_repetition(text: str) -> bool:
    words = text.split()
    for size in range(1, min(36, len(words) // 3 + 1)):
        for start in range(0, len(words) - size * 3 + 1):
            phrase = words[start : start + size]
            repeats = phrase == words[start + size : start + 2 * size] == words[start + 2 * size : start + 3 * size]
            if repeats and (size > 1 or len(words) >= 6):
                return True
    return False


def _token_disagreements(visual: str, embedded: str) -> list[dict[str, str]]:
    token_pattern = re.compile(r"\w+|[^\w\s]", re.UNICODE)
    visual_tokens = token_pattern.findall(visual)
    embedded_tokens = token_pattern.findall(embedded)
    matcher = SequenceMatcher(
        None,
        [token.casefold() for token in visual_tokens],
        [token.casefold() for token in embedded_tokens],
        autojunk=False,
    )
    disagreements: list[dict[str, str]] = []
    for operation, visual_start, visual_end, embedded_start, embedded_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        visual_value = " ".join(visual_tokens[visual_start:visual_end])
        embedded_value = " ".join(embedded_tokens[embedded_start:embedded_end])
        if not any(character.isalnum() for character in visual_value + embedded_value):
            continue
        disagreements.append({"operation": operation, "visual": visual_value, "embedded": embedded_value})
        if len(disagreements) == 50:
            break
    return disagreements
