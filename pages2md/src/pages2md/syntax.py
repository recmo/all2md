"""Source-mapped Markdown and math syntax shared by repairs and validation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin

@dataclass(frozen=True)
class MathSpan:
    start: int
    content_start: int
    content_end: int
    end: int
    display: bool


def _escaped(text: str, index: int) -> bool:
    backslashes = 0
    while index > 0 and text[index - 1] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def math_spans(text: str) -> tuple[list[MathSpan], list[int]]:
    """Find math outside Markdown code/comments, retaining exact source offsets.

    Dollar delimiters follow whitespace/digit boundaries to avoid interpreting
    ordinary currency as math. An unmatched single dollar is consequently not
    an error. Explicit unmatched TeX delimiters are reported, never consumed to
    EOF (which would hide subsequent prose from the Markdown linter).
    """
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    excluded = []
    for token in MarkdownIt("commonmark").use(footnote_plugin).parse(text):
        if token.type in {"fence", "code_block"} and token.map:
            excluded.append((offsets[token.map[0]], offsets[token.map[1]]))
    excluded.extend((m.start(), m.end()) for m in re.finditer(r"<!--.*?(?:-->|\Z)", text, re.S))
    excluded.sort()
    spans, unclosed = [], []
    index = 0
    exclusion = 0
    while index < len(text):
        while exclusion < len(excluded) and excluded[exclusion][1] <= index:
            exclusion += 1
        if exclusion < len(excluded) and excluded[exclusion][0] <= index:
            index = excluded[exclusion][1]
            continue
        limit = excluded[exclusion][0] if exclusion < len(excluded) else len(text)
        if text[index] == "`" and not _escaped(text, index):
            run = re.match(r"`+", text[index:])[0]
            close = re.search(r"(?<!`)" + re.escape(run) + r"(?!`)", text[index + len(run):limit])
            index = index + len(run) + close.end() if close else index + len(run)
            continue
        opening = next((value for value in (r"\(", r"\[", "$$", "$")
                        if text.startswith(value, index) and not _escaped(text, index)), None)
        if opening is None:
            if text.startswith((r"\)", r"\]"), index) and not _escaped(text, index):
                unclosed.append(index)
                index += 2
            else:
                index += 1
            continue
        start = index + len(opening)
        if opening == "$" and (start == len(text) or text[start].isspace()):
            index = start
            continue
        closing = {r"\(": r"\)", r"\[": r"\]", "$$": "$$", "$": "$"}[opening]
        # Do not join independent prose paragraphs around an unmatched opener.
        paragraph = re.search(r"\n[ \t]*\n", text[start:limit])
        if paragraph:
            limit = start + paragraph.start()
        cursor = start
        end = None
        while cursor < limit:
            if text.startswith(r"\verb", cursor) and not _escaped(text, cursor):
                verb = re.match(r"\\verb\*?([^A-Za-z\s])", text[cursor:limit])
                if verb:
                    stop = text.find(verb[1], cursor + verb.end(), limit)
                    if stop >= 0:
                        cursor = stop + 1
                        continue
            if text[cursor] == "%" and not _escaped(text, cursor):
                newline = text.find("\n", cursor, limit)
                cursor = limit if newline < 0 else newline + 1
                continue
            if text.startswith(closing, cursor) and not _escaped(text, cursor):
                if opening != "$" or (
                    cursor > start and not text[cursor - 1].isspace()
                    and not text.startswith("$$", cursor)
                    and (cursor + 1 == len(text) or not text[cursor + 1].isdigit())
                ):
                    end = cursor
                    break
            # Explicit math openers cannot be nested: recover at the next one.
            if text.startswith((r"\(", r"\["), cursor) and not _escaped(text, cursor):
                break
            cursor += 1
        if end is None:
            if opening != "$":
                unclosed.append(index)
            index = start
        else:
            spans.append(MathSpan(index, start, end, end + len(closing), opening in {r"\[", "$$"}))
            index = end + len(closing)
    return spans, unclosed


def mask_math(text: str, spans: list[MathSpan]) -> str:
    """Opaque inert characters, with the same length and newline positions."""
    masked = list(text)
    for span in spans:
        for index in range(span.start, span.end):
            if text[index] not in "\n\r\t":
                masked[index] = "x"
    return "".join(masked)


def non_math_ranges(text: str, *, references: bool = True) -> list[tuple[int, int]]:
    """Opaque code, links, HTML, and reference labels, in original offsets."""
    parser = MarkdownIt("commonmark").use(footnote_plugin)
    env = {}
    tokens = parser.parse(text, env)
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    ranges = [(offsets[t.map[0]], offsets[t.map[1]]) for t in tokens
              if t.map and t.type in {"code_block", "fence", "html_block"}]
    ranges.extend((offsets[r["map"][0]], offsets[r["map"][1]])
                  for r in env.get("references", {}).values() if "map" in r)
    source = mask_math(text, math_spans(text)[0])
    # Wrapping existing parser rules gives exact source spans even for nested
    # labels, escaped punctuation, and variable-length code delimiters.
    for name in ("backticks", "link", "image", "autolink", "html_inline"):
        original = parser.inline.ruler.get_active_rules()
        rule = parser.inline.ruler.getRules("")[original.index(name)]

        def located(state, silent, rule=rule):
            start = state.pos
            accepted = rule(state, silent)
            if accepted and not silent and state.src == source:
                ranges.append((start, state.pos))
            return accepted

        parser.inline.ruler.at(name, located)
    parser.parseInline(source, env)
    # Incomplete/invalid OCR link syntax is not an invitation to edit its
    # address. Keep unresolved bracket labels opaque as well.
    suffix = "?" if references else ""
    ranges.extend(m.span() for m in re.finditer(
        r"!?\[[^\]\n]*\](?:\([^\n)]*\)|\[[^\]\n]*\])" + suffix + r"|\\[A-Za-z]+", source))
    ranges.extend(m.span() for m in re.finditer(r"\[\^[^\]]+\]|<!--.*?(?:-->|\Z)", text, re.S))
    ranges.extend(m.span() for m in re.finditer(r"<a\b[^>]*>.*?</a\s*>", text, re.I | re.S))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def protected_ranges(text: str, *, references: bool = True) -> list[tuple[int, int]]:
    return sorted(non_math_ranges(text, references=references) + [(s.start, s.end) for s in math_spans(text)[0]])


def formatting_spans(text: str) -> list[tuple[int, int, bool]]:
    """Protect literal math and the containers owning paragraph-local notes."""
    parser = MarkdownIt("commonmark").use(footnote_plugin)
    parser.core.ruler.disable("footnote_tail")  # Keep definitions at their source positions.
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    tokens = parser.parse(text)
    roots = [t.map for t in tokens if t.level == 0 and t.map]
    ranges = [(s.start, s.end, False) for s in math_spans(text)[0]]
    ranges.extend((*m.span(), False) for m in re.finditer(r"\[\^[^\]]+\]", text))
    for token in tokens:
        if token.type == "footnote_reference_open" and token.map:
            start, end = next((a, b) for a, b in roots if a <= token.map[0] < b)
            ranges.append((offsets[start], offsets[end], True))
    outer = []
    for span in sorted(ranges, key=lambda s: (s[0], -s[1])):
        if not outer or span[0] >= outer[-1][1]:
            outer.append(span)
    return outer
