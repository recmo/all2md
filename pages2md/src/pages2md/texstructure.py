"""Small, source-preserving TeX tokenizer used for inspection and formatting.

This does not evaluate TeX or expand macros. Unknown commands stay opaque.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


TOKEN = re.compile(r"\\[A-Za-z]+\*?|\\[\s\S]|[{}_^]|[^\\{}_^]")
TEXT = {"text", "textbf", "textit", "textrm", "textsf", "texttt", "operatorname"}
ENVIRONMENTS = {"array", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix",
                "Vmatrix", "aligned", "alignedat", "align", "align*", "cases",
                "gathered", "split"}


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int


def tokens(value: str) -> list[Token]:
    return [Token(m.group(), *m.span()) for m in TOKEN.finditer(value)]


def _group_pairs(ts: list[Token], start: int = 0):
    """Match braces iteratively; escaped braces are opaque command tokens."""
    stack = []
    for i in range(start, len(ts)):
        if ts[i].value == "{":
            stack.append(i)
        elif ts[i].value == "}" and stack:
            yield stack.pop(), i


def group_end(ts: list[Token], index: int) -> int | None:
    if index >= len(ts) or ts[index].value != "{":
        return None
    return next((end for start, end in _group_pairs(ts, index) if start == index), None)


def next_nonspace(ts: list[Token], index: int) -> int:
    while index < len(ts) and ts[index].value.isspace():
        index += 1
    return index


@dataclass
class TexGroup:
    start: int
    end: int
    script: str | None
    children: list[TexGroup]


def tex_groups(value: str) -> list[TexGroup]:
    """Group/script tree over the same tokens and brace matching as inspection.

    Preserve the repair API's -1 end for incomplete groups; never synthesize a brace.
    """
    ts = tokens(value)

    closes = dict(_group_pairs(ts))
    groups = []
    stack = []
    pending = None
    for i, token in enumerate(ts):
        if token.value in {"_", "^"}:
            pending = token.value
        elif token.value == "{":
            close = closes.get(i)
            group = TexGroup(token.end, ts[close].start if close is not None else -1,
                             pending, [])
            (stack[-1].children if stack else groups).append(group)
            stack.append(group)
            pending = None
        elif token.value == "}":
            if stack:
                stack.pop()
            pending = None
        elif not token.value.isspace():
            pending = None
    return groups


def text_ranges(value: str) -> list[tuple[int, int]]:
    ts = tokens(value)
    ranges = []
    for i, token in enumerate(ts):
        if token.value.lstrip("\\").rstrip("*") not in TEXT:
            continue
        start = next_nonspace(ts, i + 1)
        end = group_end(ts, start)
        if end is not None:
            ranges.append((token.start, ts[end].end))
    return ranges


def script_memberships(value: str) -> dict[int, tuple[int, str]]:
    """Map visible token offsets to the base and script kind that own them."""
    ts = tokens(value)
    edges = {}
    closes = dict(_group_pairs(ts))
    scopes = [(0, len(ts), None)]
    while scopes:
        start, end, inherited = scopes.pop()
        base = None
        i = start
        while i < end:
            token = ts[i]
            if token.value.isspace():
                i += 1
                continue
            if token.value in {"_", "^"}:
                j = next_nonspace(ts, i + 1)
                close = closes.get(j)
                parent = (base, token.value) if base is not None else inherited
                if close is not None and close < end:
                    scopes.append((j + 1, close, parent))
                    i = close + 1
                elif j < end:
                    if parent:
                        edges[ts[j].start] = parent
                    i = j + 1
                else:
                    i += 1
                continue
            if token.value == "{":
                close = closes.get(i)
                if close is None:
                    break
                scopes.append((i + 1, close, inherited))
                base = token.start
                i = close + 1
                continue
            if token.value != "}":
                base = token.start
                if inherited:
                    edges[token.start] = inherited
            i += 1
    return edges


def format_environments(value: str) -> str:
    """Insert row newlines without moving optional spacing or nested groups."""
    ts = tokens(value)
    opaque = text_ranges(value)
    breaks = set()
    stack = []
    depth = 0
    i = 0
    while i < len(ts):
        t = ts[i]
        if any(a <= t.start < b for a, b in opaque):
            i += 1
            continue
        if t.value in {r"\begin", r"\end"}:
            j = next_nonspace(ts, i + 1)
            end = group_end(ts, j)
            if end is None:
                i += 1
                continue
            name = value[ts[j].end:ts[end].start]
            if t.value == r"\begin":
                stack.append((name, depth))
                k = end + 1
                if name in {"array", "alignedat"}:
                    k = next_nonspace(ts, k)
                    # array's optional vertical alignment precedes its columns.
                    if k < len(ts) and ts[k].value == "[":
                        while k < len(ts) and ts[k].value != "]":
                            k += 1
                        k = next_nonspace(ts, k + 1)
                    arg = group_end(ts, k)
                    if arg is not None:
                        end = arg
                if name in ENVIRONMENTS:
                    breaks.add(ts[end].end)
            elif stack and stack[-1][0] == name:
                if name in ENVIRONMENTS:
                    breaks.add(t.start)
                stack.pop()
            i = end + 1
            continue
        if t.value == r"\\" and stack and stack[-1][0] in ENVIRONMENTS and depth == stack[-1][1]:
            end = t.end
            j = i + 1
            if j < len(ts) and ts[j].value == "*":
                end = ts[j].end
                j += 1
            j = next_nonspace(ts, j)
            if j < len(ts) and ts[j].value == "[":
                bracket_depth = 1
                j += 1
                while j < len(ts) and bracket_depth:
                    bracket_depth += ts[j].value == "["
                    bracket_depth -= ts[j].value == "]"
                    end = ts[j].end
                    j += 1
            breaks.add(end)
        depth += t.value == "{"
        depth -= t.value == "}"
        i += 1
    for position in sorted(breaks, reverse=True):
        left = value[:position].rstrip(" \t\r\n")
        right = value[position:].lstrip(" \t\r\n")
        value = left + "\n" + right
    return value


def balanced_sizing_delimiters(value: str) -> bool:
    """Check left/right scope without interpreting delimiter shapes."""
    ts = tokens(value)
    opaque = text_ranges(value)
    scopes = [0]
    for token in ts:
        if any(a <= token.start < b for a, b in opaque):
            continue
        if token.value == "{":
            scopes.append(0)
        elif token.value == "}":
            if len(scopes) == 1 or scopes.pop():
                return False
        elif token.value == r"\left":
            scopes[-1] += 1
        elif token.value == r"\right":
            scopes[-1] -= 1
            if scopes[-1] < 0:
                return False
    return len(scopes) == 1 and scopes[0] == 0
