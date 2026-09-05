"""Apply source-offset edits once, with consistent validation and provenance."""
from __future__ import annotations

from .model import Block

TextEdit = tuple[int, int, str]


def apply_edits(block: Block, edits: list[TextEdit], kind: str) -> bool:
    source = block.markdown
    ordered = sorted(set(edits))
    if any(not 0 <= start <= end <= len(source) for start, end, _ in ordered):
        raise ValueError("edit outside source bounds")
    # Conflicting proposals abstain, rather than depending on function order.
    accepted = [edit for i, edit in enumerate(ordered)
                if not any(j != i and ((other[0] < edit[1] and edit[0] < other[1])
                           or (other[0] == edit[0] and (other[1] == other[0] or edit[1] == edit[0])))
                           for j, other in enumerate(ordered))]
    accepted = [e for e in accepted if source[e[0]:e[1]] != e[2]]
    for start, end, replacement in reversed(accepted):
        block.markdown = block.markdown[:start] + replacement + block.markdown[end:]
    block.metadata.setdefault("embedded_text_repairs", []).extend(
        {"kind": kind, "visual": source[start:end], "embedded": replacement}
        for start, end, replacement in accepted)
    if not block.metadata["embedded_text_repairs"]:
        block.metadata.pop("embedded_text_repairs")
    return bool(accepted)
