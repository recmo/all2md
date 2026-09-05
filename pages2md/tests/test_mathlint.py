from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from pages2md.formatting import _pymarkdown, format_and_lint
from pages2md.mathlint import lint_math, mask_math, math_spans, validator_identity
from pages2md.quality import math_syntax_errors


def validate(text):
    spans, unclosed = math_spans(text)
    return lint_math([(Path("book.md"), text, spans, unclosed)])


@pytest.fixture
def katex():
    if not shutil.which(os.environ.get("PAGES2MD_NODE", "node")):
        if os.environ.get("PAGES2MD_KATEX_MODULE"):
            pytest.fail("KaTeX is configured but Node.js is unavailable")
        pytest.skip("Node.js unavailable; run inside nix develop .#pages2md")
    result = validate(r"\(x\)")
    if result.status == "unavailable":
        if os.environ.get("PAGES2MD_KATEX_MODULE"):
            pytest.fail(result.diagnostics[0]["message"])
        pytest.skip(result.diagnostics[0]["message"])
    return result.engine


def test_math_scanner_recognizes_all_delimiters_and_preserves_offsets():
    text = "A $x^2$, $$y_1$$, " + r"\(z\)" + ".\n\\[\na+b\n\\]\n"
    spans, unmatched = math_spans(text)
    assert not unmatched
    assert [text[s.content_start:s.content_end] for s in spans] == ["x^2", "y_1", "z", "\na+b\n"]
    assert [s.display for s in spans] == [False, True, False, True]
    masked = mask_math(text, spans)
    assert len(masked) == len(text)
    assert [i for i, c in enumerate(text) if c == "\n"] == [i for i, c in enumerate(masked) if c == "\n"]
    assert masked.startswith("A xxxxx, xxxxxxx, xxxxx.")


def test_scanner_ignores_code_comments_escapes_and_currency():
    text = (
        "```latex\n$not math$ \\[\n```\n\n"
        "    $indented code$\n\n"
        "`$inline$` and ``code ` \\( x \\)``.\n\n"
        "<!-- \\( comment \\) -->\n\n"
        r"Escaped \$20; prices $5 and $10; \\(literal\\). "
        r"Actual \(\text{cost \$5}\) and $a+b$."
    )
    spans, unmatched = math_spans(text)
    assert not unmatched
    assert [text[s.content_start:s.content_end] for s in spans] == [r"\text{cost \$5}", "a+b"]


def test_unclosed_delimiter_does_not_hide_later_prose():
    text = "Broken \\(x\n\nA * bad * example.\n\nGood \\(y\\).\n"
    spans, unmatched = math_spans(text)
    assert unmatched == [text.index(r"\(")]
    assert len(spans) == 1
    assert "* bad *" in mask_math(text, spans)


def test_tex_comments_do_not_close_a_formula():
    text = "\\[x % ignore \\]\n + y\\]"
    spans, unmatched = math_spans(text)
    assert len(spans) == 1 and not unmatched
    assert spans[0].end == len(text)


def test_tex_verbatim_does_not_close_a_formula():
    text = r"\(\verb|\)|+x\)"
    spans, unmatched = math_spans(text)
    assert len(spans) == 1 and not unmatched
    assert spans[0].end == len(text)


def test_legacy_quality_checks_use_the_same_math_boundaries():
    assert math_syntax_errors("```tex\n\\(x^{\n```\n") == []
    assert math_syntax_errors(r"Code `\[x` costs $5 and $10.") == []
    assert math_syntax_errors(r"Real $x^{$.") == ["math span 1 has unbalanced braces"]


def test_markdown_lint_ignores_math_but_preserves_prose_diagnostic_columns():
    pytest.importorskip("pymarkdown")
    text = "# Test\n\n" + r"Math \(a * b * c\), prose * bad * example." + "\n"
    spans, _ = math_spans(text)
    result = _pymarkdown(mask_math(text, spans))
    warnings = result.stdout.splitlines()
    assert len(warnings) == 2
    for warning in warnings:
        _, line, column, *_ = warning.split(":")
        assert line == "3"
        assert int(column) >= text.splitlines()[2].index("prose") + 1
        assert "MD037" in warning


def test_real_lint_and_format_never_modify_formula(tmp_path, katex):
    pytest.importorskip("pymarkdown")
    text = "# Test\n\n" + r"An expression \(a * b  + c\)." + "\n"
    path = tmp_path / "book.md"
    path.write_text(text)
    result = format_and_lint([path])
    assert result.math_validation.checked == 1
    assert not result.math_validation.diagnostics
    assert not result.lint_errors
    assert path.read_text() == text


def test_katex_checks_syntax_and_separates_unsupported_commands(katex):
    result = validate(r"\(x^2\) \(x^{\) \(\notARealCommand\) \(\begin{notARealEnvironment}x\end{notARealEnvironment}\)")
    assert result.engine == katex
    assert result.status == "checked" and result.checked == 4
    assert [d["category"] for d in result.diagnostics] == ["syntax", "unsupported", "unsupported"]
    assert all(d["path"] == "book.md" for d in result.diagnostics)


def test_katex_locations_handle_multiline_math_and_astral_unicode(katex):
    text = '# Math\n\n\\[\n\\text{😀}+\\notARealCommand\n\\]\n'
    result = validate(text)
    finding, = result.diagnostics
    assert finding["line"] == 4
    assert finding["column"] == text.splitlines()[3].index(r"\notARealCommand") + 1


def test_macro_state_cannot_leak_between_formulas(katex):
    result = validate(r"\(\gdef\custom{x}\custom\) \(\custom\)")
    assert result.checked == 2
    finding, = result.diagnostics
    assert finding["category"] == "unsupported"


def test_untrusted_commands_and_runaway_macros_are_not_executed(katex):
    result = validate(r"\(\includegraphics{https://example.invalid/image}\) \(\def\a{\a}\a\)")
    assert [d["category"] for d in result.diagnostics] == ["unsupported", "resource_limit"]


def test_missing_validator_is_reported_not_silently_passed(monkeypatch):
    monkeypatch.setenv("PAGES2MD_NODE", "/no/such/node")
    result = validate(r"\(x\)")
    assert result.status == "unavailable"
    assert result.checked == 0
    assert result.diagnostics[0]["category"] == "validator_unavailable"
    assert validator_identity() == {"katex": None}


def test_validator_fingerprint_includes_the_actual_engine_version(katex):
    assert validator_identity() == {"katex": katex.split("==")[1]}


def test_delimiter_diagnostics_do_not_require_node(monkeypatch):
    monkeypatch.setenv("PAGES2MD_NODE", "/no/such/node")
    result = validate("# Text\n\nUnclosed \\(x.\n")
    assert result.status == "not_needed"
    assert result.diagnostics == [{"path": "book.md", "line": 3, "column": 10,
                                  "category": "syntax", "message": "Unmatched math delimiter"}]
