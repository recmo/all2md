from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import mdformat

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


def format_markdown(text: str) -> str:
    return mdformat.text(
        text,
        options={"wrap": "keep", "number": False},
        extensions={"gfm"},
    )


def format_and_lint(paths: list[Path]) -> FormatResult:
    result = FormatResult()
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
    scan = _pymarkdown("scan", paths, check=False)
    if scan.returncode:
        result.lint_errors = [line for line in scan.stdout.splitlines() if line.strip()]
    return result


def is_formatted_idempotently(text: str) -> bool:
    once = format_markdown(text)
    if _structural_signature(once) != _structural_signature(text):
        return True
    return once == text and format_markdown(once) == once


def _structural_signature(text: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    pages = tuple(re.findall(r"<!-- page: (\d+) -->", text))
    images = tuple(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text))
    tokens = tuple(re.findall(r"[\w]+|[^\w\s]", text, re.UNICODE))
    return pages, images, tokens


def _pymarkdown(command: str, paths: list[Path], *, check: bool):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pymarkdown",
            "-d",
            DISABLED_LINT_RULES,
            command,
            *map(str, paths),
        ],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
