from __future__ import annotations

import fitz

from pages2md.adapters import _is_page_backing_image
from pages2md.model import Block, Link
from pages2md.document import (
    apply_links_to_blocks,
)
from pages2md.quality import (
    adjacent_overlap,
    math_syntax_errors,
    output_quality_warnings,
    table_quality_errors,
    truncate_runaway_repetition,
)


def test_runaway_generation_is_rejected_independently_of_model_confidence():
    markdown = " ".join(["e.g. 1", "e.g. 2", "e.g. 3"] * 2_000)
    warnings = output_quality_warnings(markdown)
    assert "visual_text_repetition" in warnings
    assert "visual_implausible_output_length" in warnings


def test_runaway_generation_is_truncated_after_one_cycle():
    markdown = (
        "".join(f"Answer\nUnique section {index}\n" for index in range(5))
        + ("loop phrase " * 20)
        + "\nLegitimate conclusion."
    )
    repaired, changed = truncate_runaway_repetition(markdown)
    assert changed is True
    assert repaired.startswith("Answer\nUnique section 0")
    assert "Unique section 4" in repaired
    assert repaired.count("loop phrase") == 1
    assert repaired.endswith("Legitimate conclusion.")
    assert "visual_text_repetition" not in output_quality_warnings(repaired)


def test_single_token_repetition_is_review_only():
    markdown = " ".join(["A"] * 12)
    repaired, changed = truncate_runaway_repetition(markdown)
    assert "visual_text_repetition" in output_quality_warnings(markdown)
    assert changed is False
    assert repaired == markdown


def test_repetitive_table_is_semantically_invalid():
    rows = "".join(
        f"<tr><td>[{number}]</td><td>[{number}]</td><td>[{number}]</td></tr>"
        for number in range(500)
    )
    assert any("implausibly many rows" in error for error in table_quality_errors(f"<table>{rows}</table>"))


def test_dense_rectangular_table_is_not_mistaken_for_collapse():
    rows = "".join(
        f"<tr><td>{number:07d}</td><td>{1093 if number % 2 else 2186}</td></tr>"
        for number in range(150)
    )
    assert table_quality_errors(f"<table>{rows}</table>") == []


def test_repeated_figure_markup_is_not_mistaken_for_text_repetition():
    markdown = "\n".join(
        f"![Figure](figures/fig-{index:04d}.png)"
        for index in range(30)
    )
    assert "visual_text_repetition" not in output_quality_warnings(markdown)


def test_math_validator_catches_local_unclosed_delimiter():
    assert math_syntax_errors(r"<td>\( a_2</td>") == [r"unbalanced math delimiter \("]
    assert math_syntax_errors(r"The value is \(a_2^3\).") == []
    assert math_syntax_errors(
        r"\[\begin{array}\begin{matrix}a\end{matrix}\end{array}\]"
    ) == []


def test_adjacent_overlap_detects_a_continuation_emitted_twice():
    continuation = "whose matrix entries form a field over F2 " * 20
    assert adjacent_overlap("Exercise 2.59 " + continuation, continuation)


def test_adjacent_overlap_detects_repeated_table_rows():
    repeated = "".join(f"<tr><td>{number}</td><td>{number + 1}</td></tr>" for number in range(12))
    first = f"<table>{repeated}</table>"
    second = f"<table>{repeated}<tr><td>new</td><td>row</td></tr></table>"
    assert adjacent_overlap(first, second)


def test_embedded_links_only_wrap_plain_text_nodes():
    block = Block(
        "paragraph",
        "See [42](#page-57), not 142, then see 42 again.",
    )
    apply_links_to_blocks([block], [Link(text="42", target="#page-42")])
    assert block.markdown == "See [42](#page-57), not 142, then see [42](#page-42) again."
    apply_links_to_blocks([block], [Link(text="42", target="#page-42")])
    assert block.markdown.count("#page-42") == 1


def test_page_backing_scan_is_not_a_figure():
    page = fitz.Rect(0, 0, 600, 800)
    assert _is_page_backing_image(fitz.Rect(0, 0, 600, 800), page)
    assert not _is_page_backing_image(fitz.Rect(100, 100, 500, 500), page)
