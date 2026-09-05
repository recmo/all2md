from __future__ import annotations

import pytest

from pages2md.model import Block
from pages2md.alignment import (
    semantic_math_projection as project,
)
from pages2md.semantics import accent_identity, repair_accents, restore_inline_math
from test_alignment import BOX, evidence


@pytest.mark.parametrize(("mark", "font", "command"), [
    ("®", "ABCDEF+txsys", "vec"), ("\u20d7", "Unknown", "vec"),
    ("\u0302", "Unknown", "hat"), ("\u0303", "Unknown", "tilde"),
    ("\u0304", "Unknown", "bar"), ("\u0307", "Unknown", "dot"),
    ("\u0308", "Unknown", "ddot"),
])
def test_accents_are_attached_to_arbitrary_letters(mark, font, command):
    native = evidence([("value=", 10, 100, 10, "Times-Roman"),
                       ("z", 40, 100, 10, "NewTXMI"),
                       (mark, 40, 98, 10, font),
                       ("+1", 45, 100, 10, "Times-Roman")])
    wrong = "hat" if command == "tilde" else "tilde"
    block = Block("formula", rf"\[value=\{wrong}{{z}}+1\]", bbox=BOX)
    assert repair_accents([block], native, project)
    assert block.markdown == rf"\[value=\{command}{{z}}+1\]"
    assert repair_accents([block], native, project) == []


@pytest.mark.parametrize("font", ["txexs", "Times-Roman", "unknown", "not-txsys"])
def test_registered_sign_is_not_an_accent_without_known_encoding(font):
    assert accent_identity({"text": "®", "font": font}) is None


def test_ambiguous_accent_attachment_and_code_are_unchanged():
    native = evidence([("z", 40, 100, 10, "NewTXMI"),
                       ("z", 40, 100, 10, "NewTXMI"),
                       ("®", 40, 100, 10, "txsys")])
    block = Block("formula", r"\(\tilde{z}\)", bbox=BOX)
    assert repair_accents([block], native, project) == []
    native = evidence([("z", 40, 100, 10, "NewTXMI"), ("®", 40, 100, 10, "txsys")])
    block = Block("paragraph", r"`\(\tilde{z}\)`", bbox=BOX)
    assert repair_accents([block], native, project) == []


def test_inline_math_uses_math_glyphs_not_ordinary_italics_or_article_a():
    native = evidence([("Use a ", 10, 100, 10, "Times-Italic"),
                       ("a", 40, 100, 10, "NewTXMI"),
                       (" and ", 45, 100, 10, "Times-Italic"),
                       ("mA", 70, 100, 10, "NewTXMI"),
                       (" here.", 80, 100, 10, "Times-Italic")])
    block = Block("paragraph", "Use a a and mA here.", bbox=BOX)
    assert restore_inline_math([block], native, project)
    assert block.markdown == r"Use a \(a\) and \(mA\) here."
    assert restore_inline_math([block], native, project) == []


def test_inline_math_wraps_complete_arithmetic_expression():
    native = evidence([("degree ", 10, 100, 10, "Times-Roman"),
                       ("k", 45, 100, 10, "NewTXMI"),
                       (" - 1", 50, 100, 10, "Times-Roman")])
    block = Block("paragraph", "degree k - 1", bbox=BOX)
    restore_inline_math([block], native, project)
    assert block.markdown == r"degree \(k - 1\)"


@pytest.mark.parametrize("text", [r"`a`", r"[a](https://a.test)", r"\(a\)", r"$a$", "[a]", "```\na\n```"])
def test_inline_math_protects_existing_syntax(text):
    native = evidence([("a", 40, 100, 10, "NewTXMI")])
    block = Block("paragraph", text, bbox=BOX)
    assert restore_inline_math([block], native, project) == []
    assert block.markdown == text


def test_missing_evidence_and_partial_words_abstain():
    native = evidence([("c", 10, 100, 10, "NewTXMI"), ("at", 15, 100, 10, "Times-Roman")])
    block = Block("paragraph", "cat", bbox=BOX)
    assert restore_inline_math([block], native, project) == []
    assert restore_inline_math([Block("paragraph", "c")], native, project) == []


def test_inline_math_updates_structured_list_source_and_is_idempotent():
    from pages2md.lists import render_list
    native = evidence([("Use ", 10, 100, 10, "Times-Roman"), ("x", 30, 100, 10, "NewTXMI")])
    node = {"marker_style": "decimal", "items": [
        {"source_ordinal": 1, "blocks": [{"kind": "paragraph", "markdown": "Use x"}]}]}
    block = Block("list", "1. Use x", bbox=BOX, metadata={"list": node})
    assert restore_inline_math([block], native, project)
    assert render_list(node) == block.markdown == r"1. Use \(x\)"
    assert restore_inline_math([block], native, project) == []
