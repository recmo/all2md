"""Read-only, source-mapped validation of math embedded in Markdown."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin


@dataclass(frozen=True)
class MathSpan:
    start: int
    content_start: int
    content_end: int
    end: int
    display: bool


@dataclass
class MathLintResult:
    status: str = "not_needed"
    engine: str | None = None
    checked: int = 0
    diagnostics: list[dict] = field(default_factory=list)


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


def _diagnostic(path: Path, text: str, offset: int, category: str, message: str) -> dict:
    return {"path": str(path), "line": text.count("\n", 0, offset) + 1,
            "column": offset - text.rfind("\n", 0, offset),
            "category": category, "message": message}


def _run_katex(expressions: list[dict]) -> dict:
    command = [os.environ.get("PAGES2MD_NODE", "node"), "--max-old-space-size=128",
               str(Path(__file__).with_name("katex_lint.cjs"))]
    scan = subprocess.run(command, input=json.dumps(expressions), text=True,
                          capture_output=True, timeout=30, check=True)
    return json.loads(scan.stdout)


def validator_identity() -> dict:
    """Installing/upgrading KaTeX invalidates assembly, never cached model OCR."""
    try:
        return {"katex": _run_katex([])["version"]}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return {"katex": None}


def lint_math(documents: list[tuple[Path, str, list[MathSpan], list[int]]]) -> MathLintResult:
    result = MathLintResult()
    expressions = []
    locations = []
    for path, text, spans, unclosed in documents:
        result.diagnostics.extend(_diagnostic(path, text, index, "syntax", "Unmatched math delimiter")
                                  for index in unclosed)
        for span in spans:
            expressions.append({"tex": text[span.content_start:span.content_end], "display": span.display})
            locations.append((path, text, span))
    if not expressions:
        return result
    try:
        payload = _run_katex(expressions)
        if len(payload["results"]) != len(expressions):
            raise ValueError("KaTeX returned an incomplete result")
        result.engine = f"katex=={payload['version']}"
        for (path, text, span), findings in zip(locations, payload["results"]):
            for finding in findings:
                # KaTeX offsets count UTF-16 code units, Python counts Unicode
                # code points. Astral symbols before an error must not shift it.
                tex = text[span.content_start:span.content_end]
                units = max(0, int(finding.get("position") or 0))
                local = len(tex.encode("utf-16-le")[:2 * units].decode("utf-16-le", errors="ignore"))
                result.diagnostics.append(_diagnostic(
                    path, text, span.content_start + local, finding["category"], finding["message"]
                ))
        result.status = "checked"
        result.checked = len(expressions)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        result.status = "unavailable"
        detail = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            detail = error.stderr.strip()[:500]
        result.diagnostics.append({"category": "validator_unavailable", "message":
            f"KaTeX validation did not complete: {detail}. Use the pages2md Nix environment "
            "or install Node.js and pages2md's npm dependencies."})
    return result
