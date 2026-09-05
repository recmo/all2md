from __future__ import annotations

import pytest

from pages2md.footnotes import footnote_definitions, normalize_footnotes, place_footnotes
from pages2md.formatting import format_and_lint, format_markdown
from pages2md.markdown import strict_page_markdown, write_markdown
from pages2md.model import Block, Chapter, Comparison, EmbeddedEvidence, PageResult
from pages2md.pipeline import _normalize_document_blocks, _semantic_math_projection as project
from pages2md.mathlint import math_spans
from test_alignment import evidence


def page(number=1, embedded=None, note_kind="page_footnote"):
    return PageResult(number, "p.png", "Some prose with a note and more text.", [
        Block("paragraph", r"Some prose\(^{1}\) with a note", bbox=(0, 70, 200, 110)),
        Block(note_kind, r"\(^{1}\) A detailed explanatory note.", bbox=(0, 790, 250, 850)),
    ], embedded or EmbeddedEvidence(), Comparison())


def native_note(marker_baseline=95, body_font="Times-Roman"):
    return evidence([
        ("Some prose", 10, 100, 10, body_font), ("1", 60, marker_baseline, 7, "Times-Roman"),
        (" with a note", 64, 100, 10, body_font),
        ("1", 10, 806, 6, "Times-Roman"), ("A detailed explanatory note.", 14, 810, 8, "Times-Roman"),
    ])


def test_ocr_labelled_notes_work_without_embedded_text():
    p = page()
    normalize_footnotes([p], project)
    assert p.blocks[0].markdown == "Some prose[^p1-note-1] with a note"
    assert footnote_definitions([p]) == "[^p1-note-1]: A detailed explanatory note."
    before = [b.markdown for b in p.blocks]
    normalize_footnotes([p], project)
    assert [b.markdown for b in p.blocks] == before


def test_geometry_recovers_unlabelled_small_note():
    p = page(embedded=native_note(), note_kind="paragraph")
    normalize_footnotes([p], project)
    assert p.blocks[1].metadata.get("footnote")


@pytest.mark.parametrize(("baseline", "font"), [(100, "Times-Roman"), (95, "NewTXMI")])
def test_normal_digits_and_math_exponents_are_not_footnote_references(baseline, font):
    p = page(embedded=native_note(baseline, font), note_kind="paragraph")
    normalize_footnotes([p], project)
    assert not p.blocks[1].metadata.get("footnote")


def test_small_caption_without_matching_reference_is_not_a_note():
    p = page(embedded=native_note(), note_kind="paragraph")
    p.blocks[1].markdown = "2 A detailed explanatory caption."
    normalize_footnotes([p], project)
    assert not p.blocks[1].metadata.get("footnote")


def test_ambiguous_duplicate_notes_abstain():
    p = page()
    p.blocks.append(Block("page_footnote", "1 Another explanatory note."))
    normalize_footnotes([p], project)
    assert not any(b.metadata.get("footnote") for b in p.blocks)


def test_footnotes_are_outside_body_and_identifiers_do_not_collide(tmp_path):
    pages = [page(1), page(2)]
    normalize_footnotes(pages, project)
    for p in pages:
        p.visual_markdown = strict_page_markdown(p, [])
        assert "explanatory" not in p.visual_markdown
    write_markdown(tmp_path, pages, [], split=False, title="Example")
    text = (tmp_path / "book.md").read_text()
    assert text.index("[^p1-note-1]:") < text.index("<!-- page: 2 -->")
    assert "[^p2-note-1]:" in text
    formatted = format_markdown(text)
    assert format_markdown(formatted) == formatted
    assert "\\[^" not in formatted
    result = format_and_lint([tmp_path / "book.md"])
    assert not result.lint_errors
    assert (tmp_path / "book.md").read_text().index("[^p1-note-1]:") < (tmp_path / "book.md").read_text().index("<!-- page: 2 -->")
    write_markdown(tmp_path, pages, [Chapter("One", 1, 1, "one"), Chapter("Two", 2, 2, "two")], split=True, title="Example")
    chapter = (tmp_path / "chapters/000-one.md").read_text()
    assert "[^p1-note-1]:" in chapter and "[^p2-note-1]" not in chapter


def test_cross_page_prose_can_join_without_swallowing_note():
    p = page()
    q = PageResult(2, "p.png", "continued here.", [Block("paragraph", "continued here.")], EmbeddedEvidence(), Comparison())
    normalize_footnotes([p, q], project)
    _normalize_document_blocks([p, q])
    assert p.blocks[0].markdown.endswith("with a note continued here.")
    assert p.blocks[1].markdown == "A detailed explanatory note."


def test_math_in_indented_footnote_is_not_mistaken_for_code():
    text = "Text[^a].\n\n[^a]: Note.\n\n    \\(x^2\\)\n"
    spans, unclosed = math_spans(text)
    assert len(spans) == 1 and not unclosed
    assert text[spans[0].content_start:spans[0].content_end] == "x^2"


def test_author_asterisk_can_be_extracted_as_math_asterisk():
    p = page(embedded=evidence([
        ("Author", 10, 100, 10, "Times-Roman"), ("∗", 40, 95, 7, "Times-Roman"),
    ]))
    note = evidence([("*A detailed explanatory note.", 10, 810, 8, "Times-Roman")])
    p.embedded.blocks.extend(note.blocks)
    p.embedded.text += note.text
    p.blocks[0].markdown = "Author*"
    p.blocks[1].markdown = "*A detailed explanatory note."
    normalize_footnotes([p], project)
    assert p.blocks[0].markdown == "Author[^p1-note-1]"


@pytest.mark.parametrize("text", [r"`Some prose\(^{1}\)`", r"[Some prose\(^{1}\)](https://example.com)", "*emphasis*"])
def test_ocr_only_footnotes_do_not_rewrite_code_links_or_emphasis(text):
    p = page()
    p.blocks[0].markdown = text
    if text == "*emphasis*":
        p.blocks[1].markdown = "*A detailed explanatory note."
    normalize_footnotes([p], project)
    assert p.blocks[0].markdown == text
    assert not p.blocks[1].metadata.get("footnote")


def test_flattened_numeric_reference_requires_native_superscript_geometry():
    p = page(embedded=native_note())
    p.blocks[0].markdown = "Some prose1 with a note"
    normalize_footnotes([p], project)
    assert p.blocks[0].markdown == "Some prose[^p1-note-1] with a note"
    p = page()
    p.blocks[0].markdown = "Some prose1 with a note"
    normalize_footnotes([p], project)
    assert p.blocks[0].markdown == "Some prose1 with a note"


def test_note_identifiers_avoid_existing_reference_ids():
    p = page()
    p.blocks.append(Block("paragraph", "Existing[^p1-note-1]"))
    normalize_footnotes([p], project)
    assert p.blocks[1].metadata["footnote"]["id"] == "p1-note-2"


def test_definitions_follow_full_paragraph_and_only_first_reference():
    p = page()
    normalize_footnotes([p], project)
    source = "First[^p1-note-1]\ncontinued line.\n\nNext[^p1-note-1].\n"
    assert place_footnotes(source, [p]) == (
        "First[^p1-note-1]\ncontinued line.\n\n"
        "[^p1-note-1]: A detailed explanatory note.\n\nNext[^p1-note-1].\n")


def test_code_references_do_not_claim_definitions():
    p = page()
    normalize_footnotes([p], project)
    source = "```\nFake[^p1-note-1]\n```\n\n`Fake[^p1-note-1]`\n\nReal[^p1-note-1].\n"
    output = place_footnotes(source, [p])
    assert output.startswith(source)
    assert output.count("[^p1-note-1]:") == 1


def test_multiple_notes_follow_reference_order_not_page_order():
    p, q = page(1), page(2)
    normalize_footnotes([p, q], project)
    output = place_footnotes("Text[^p2-note-1] and[^p1-note-1].\n\nNext.\n", [p, q])
    assert output.index("[^p2-note-1]:") < output.index("[^p1-note-1]:") < output.index("Next.")
