from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import mdformat

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


def format_markdown(text: str) -> str:
    return mdformat.text(
        text,
        options={"wrap": "keep", "number": False},
        extensions={"gfm"},
    )


def format_and_lint(paths: list[Path]) -> FormatResult:
    result = FormatResult()
    for path in paths:
        first = format_markdown(path.read_text(encoding="utf-8"))
        path.write_text(first, encoding="utf-8")
    _pymarkdown("fix", paths, check=False)
    for path in paths:
        second = format_markdown(path.read_text(encoding="utf-8"))
        path.write_text(second, encoding="utf-8")
        if format_markdown(second) != second:
            result.idempotent = False
    scan = _pymarkdown("scan", paths, check=False)
    if scan.returncode:
        result.lint_errors = [line for line in scan.stdout.splitlines() if line.strip()]
    return result


def is_formatted_idempotently(text: str) -> bool:
    once = format_markdown(text)
    return once == text and format_markdown(once) == once


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
