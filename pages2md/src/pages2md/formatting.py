from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import mdformat

from .mathlint import MathLintResult, lint_math
from .syntax import formatting_spans, mask_math, math_spans
from .util import atomic_text

DISABLED_LINT_RULES = ",".join(
    (
        "MD013",  # OCR prose and tables must not be semantically wrapped.
        "MD024",  # Repeated section names are valid across a book.
        "MD033",  # Faithful complex-table fallback is intentional HTML.
        "MD036",  # Italic figure captions are intentional.
    )
)


@dataclass
class FormatResult:
    idempotent: bool = True
    lint_errors: list[str] = field(default_factory=list)
    preservation_skips: list[str] = field(default_factory=list)
    math_validation: MathLintResult = field(default_factory=MathLintResult)


def format_markdown(text: str) -> str:
    # The formatter owns prose layout, not TeX spelling or note placement.
    # Opaque slots keep those contracts intact before formatting, rather than
    # rejecting formatting for the entire document afterwards.
    prefix = "PAGES2MDPROTECTED"
    while prefix in text:
        prefix += "X"
    replacements = []
    masked = text
    for index, (start, end, block) in reversed(list(enumerate(formatting_spans(text)))):
        key = f"{prefix}{index}TOKEN"
        marker = f"<!-- {key} -->" if block else key
        replacements.append((marker, text[start:end].rstrip("\n") if block else text[start:end]))
        masked = masked[:start] + marker + ("\n\n" if block else "") + masked[end:]
    formatted = mdformat.text(
        masked,
        options={"wrap": "keep", "number": True},
        extensions={"gfm", "footnote"},
    )
    for marker, original in replacements:
        formatted = formatted.replace(marker, original)
    return formatted


def format_and_lint(paths: list[Path]) -> FormatResult:
    result = FormatResult()
    documents = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        formatted = format_markdown(source)
        if _structural_signature(formatted) != _structural_signature(source):
            formatted = source
            result.preservation_skips.append(str(path))
        else:
            atomic_text(path, formatted)
        if format_markdown(formatted) != formatted:
            reformatted = format_markdown(formatted)
            if _structural_signature(reformatted) == _structural_signature(formatted):
                result.idempotent = False
        spans, unclosed = math_spans(formatted)
        documents.append((path, formatted, spans, unclosed))
        scan = _pymarkdown(mask_math(formatted, spans))
        if scan.returncode:
            result.lint_errors.extend(
                str(path) + line[len("stdin"):] if line.startswith("stdin:") else line
                for line in scan.stdout.splitlines() if line.strip()
            )
            if scan.returncode != 1 or not scan.stdout.strip():
                result.lint_errors.append(f"{path}: Markdown linter failed (exit {scan.returncode})")
    result.math_validation = lint_math(documents)
    return result


def is_formatted_idempotently(text: str) -> bool:
    once = format_markdown(text)
    if _structural_signature(once) != _structural_signature(text):
        return True
    return once == text and format_markdown(once) == once


def _structural_signature(text: str) -> tuple:
    pages = tuple(re.findall(r"<!-- page: (\d+) -->", text))
    images = tuple(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text))
    tokens = tuple(re.findall(r"[\w]+|[^\w\s]", text, re.UNICODE))
    # Formatting may not alter a formula, including significant TeX spaces.
    spans, _ = math_spans(text)
    math = tuple(text[span.start:span.end] for span in spans)
    return pages, images, tokens, math


def _pymarkdown(text: str):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pymarkdown",
            "-d",
            DISABLED_LINT_RULES,
            "scan-stdin",
        ],
        check=False,
        input=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
