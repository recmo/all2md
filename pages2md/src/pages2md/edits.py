"""Apply source-offset edits once, with consistent validation and provenance."""
from __future__ import annotations

from .model import Block

TextEdit = tuple[int, int, str]


def apply_edits(block: Block, edits: list[TextEdit], kind: str) -> bool:
    source = block.markdown
    ordered = sorted(set(edits))
    if any(not 0 <= start <= end <= len(source) for start, end, _ in ordered):
        raise ValueError("edit outside source bounds")
    ordered = [e for e in ordered if source[e[0]:e[1]] != e[2]]

    def overlaps(left: TextEdit, right: TextEdit) -> bool:
        a, b, _ = left
        c, d, _ = right
        return (a < d and c < b) or (a == c and (a == b or c == d))

    # Conflicting proposals within this batch abstain.
    accepted = [edit for edit in ordered
                if not any(other != edit and overlaps(edit, other) for other in ordered)]
    for start, end, replacement in reversed(accepted):
        block.markdown = block.markdown[:start] + replacement + block.markdown[end:]
    block.metadata.setdefault("embedded_text_repairs", []).extend(
        {"kind": kind, "visual": source[start:end], "embedded": replacement}
        for start, end, replacement in accepted)
    if not block.metadata["embedded_text_repairs"]:
        block.metadata.pop("embedded_text_repairs")
    return bool(accepted)
