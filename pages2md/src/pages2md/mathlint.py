"""Read-only, source-mapped validation of math embedded in Markdown."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .syntax import MathSpan


@dataclass
class MathLintResult:
    status: str = "not_needed"
    engine: str | None = None
    checked: int = 0
    diagnostics: list[dict] = field(default_factory=list)


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
