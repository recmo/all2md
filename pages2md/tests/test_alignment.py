from __future__ import annotations

import fitz
import pytest

from pages2md.adapters import _raw_text_blocks
from pages2md.alignment import align_glyphs, math_font_role, ordered_glyphs, script_edits, tex_groups
from pages2md.embedded import assess_embedded, iter_embedded_characters
from pages2md.model import Block, EmbeddedEvidence
from pages2md.reconciliation import (
    _repair_embedded_math_glyphs,
    _repair_embedded_math_structure,
    _restore_embedded_math_alphabets,
)
from pages2md.pipeline import (
    _restore_embedded_proof_marks,
)
from pages2md.alignment import (
    semantic_math_projection as _semantic_math_projection,
)


BOX = (0, 0, 1000, 1000)


def evidence(rows):
    """Rows of (text, x, baseline, font size, font), with complete glyph data."""
    spans = []
    for text, x, baseline, size, font in rows:
        chars = []
        for letter in text:
            chars.append({"text": letter, "origin": [x, baseline],
                          "bbox": [x, baseline - size, x + size * .5, baseline]})
            x += size * .5
        spans.append({"text": text, "font": font, "size": size,
                      "em": [size, size], "chars": chars})
    text = "".join(row[0] for row in rows)
    return EmbeddedEvidence(text=text, blocks=[{
        "text": text, "bbox": list(BOX), "lines": [{"spans": spans}],
    }])


def test_extractor_retains_baselines_and_font_scale():
    doc = fitz.open()
    page = doc.new_page(width=200, height=400)
    page.insert_text((20, 60), "Hello", fontsize=12)
    blocks = _raw_text_blocks(page)
    line = blocks[0]["lines"][0]
    assert line["direction"] == [1, 0]
    span = line["spans"][0]
    assert span["em"] == [60, 30]
    assert span["chars"][0]["origin"] == [100, 150]


def test_context_distinguishes_repeated_letters_on_close_baselines():
    native = evidence([
        ("Thus A counts terms.", 20, 100, 10, "Times-Roman"),
        ("Each term of A expands.", 20, 112, 10, "Times-Roman"),
    ])
    spans = native.blocks[0]["lines"][0]["spans"]
    # Split the styled glyph out of its prose span.
    char = spans[0]["chars"].pop(5)
    spans.append({"font": "txsys", "size": 10, "em": [10, 10], "chars": [char]})
    char = spans[1]["chars"].pop(13)
    spans.append({"font": "NewTXMI", "size": 10, "em": [10, 10], "chars": [char]})
    block = Block("paragraph", r"Thus A counts terms. Each term of \(\mathcal{A}\) expands.", bbox=BOX)
    _restore_embedded_math_alphabets([block], native)
    assert block.markdown == r"Thus \(\mathcal {A}\) counts terms. Each term of \(A\) expands."
    original = block.markdown
    assert _restore_embedded_math_alphabets([block], native) == []
    assert block.markdown == original


@pytest.mark.parametrize(("font", "letter", "role"), [
    ("ABCDEF+txsym", "F", "mathbb"),
    ("txsys", "Q", "mathcal"),
    ("NewTXMI", "Q", "ordinary"),
    ("unknown", "𝒬", "mathcal"),
    ("unknown", "ℤ", "mathbb"),
    ("unknown", "Q", None),
    ("txsys", "∑", None),
    ("not-a-cmsy-font", "Q", None),
])
def test_font_decoding_is_per_glyph_not_whole_symbol_font(font, letter, role):
    assert math_font_role({"font": font, "text": letter}) == role


def test_ambiguous_repeated_occurrence_does_not_borrow_a_font():
    native = evidence([("A", 20, 100, 10, "txsys")])
    block = Block("formula", r"\(A + A\)", bbox=BOX)
    assert _restore_embedded_math_alphabets([block], native) == []
    assert block.markdown == r"\(A + A\)"


def test_full_context_can_correct_a_styled_letter():
    native = evidence([("The target is ", 20, 100, 10, "Times-Roman"),
                       ("I", 90, 100, 10, "txsys"),
                       (" for all inputs.", 95, 100, 10, "Times-Roman")])
    block = Block("paragraph", r"The target is \(T\) for all inputs.", bbox=BOX)
    _restore_embedded_math_alphabets([block], native)
    assert block.markdown == r"The target is \(\mathcal {I}\) for all inputs."


def test_nested_script_recovery_is_based_on_parent_edges():
    native = evidence([
        ("a=Y", 20, 100, 10, "NewTXMI"),
        ("r", 35, 103, 7, "NewTXMI"),
        ("u", 35, 95, 7, "NewTXMI"),
        ("v", 39, 96.4, 5, "NewTXMI"),
        ("+1", 45, 100, 10, "NewTXMI"),
    ])
    block = Block("formula", r"\[a=Y_{r}^{u / v}+1\]", bbox=BOX)
    _repair_embedded_math_structure([block], native)
    assert block.markdown == r"\[a=Y_{r}^{u_{v}}+1\]"
    assert _repair_embedded_math_structure([block], native) == []


def test_nested_script_geometry_resolves_interleaved_drawing_order():
    native = evidence([
        ("a=Y", 20, 100, 10, "NewTXMI"),
        ("u", 35, 95, 7, "NewTXMI"),
        ("r", 39, 96.4, 5, "NewTXMI"),
        ("r", 35, 103, 7, "NewTXMI"),
        ("+1", 45, 100, 10, "NewTXMI"),
    ])
    block = Block("formula", r"\[a=Y_{r}^{u / r}+1\]", bbox=BOX)
    _repair_embedded_math_structure([block], native)
    assert block.markdown == r"\[a=Y_{r}^{u_{r}}+1\]"


def test_unique_equation_context_survives_multiline_layout():
    native = evidence([
        ("a+b+c+d+e+f", 200, 90, 10, "NewTXMI"),
        ("M", 20, 110, 10, "txsys"),
        ("=span", 25, 110, 10, "Times-Roman"),
        ("g+h+i+j+k+l", 200, 110, 10, "NewTXMI"),
        ("m+n+o+p+q+r", 200, 130, 10, "NewTXMI"),
    ])
    block = Block("formula", r"\[M=\operatorname{span}\{a+b+c+d+e+f;g+h+i+j+k+l;m+n+o+p+q+r\}\]", bbox=BOX)
    _restore_embedded_math_alphabets([block], native)
    assert block.markdown.startswith(r"\[\mathcal {M}=\operatorname{span}")


def test_complete_native_line_recovers_an_ocr_clipped_delimiter():
    native = evidence([("value=⌈u+v⌉+x", 20, 100, 10, "NewTXMI")])
    block = Block("formula", r"\[value=\lceil u+v\rfloor+x\]", bbox=(15, 80, 67, 110))
    _repair_embedded_math_glyphs([block], native)
    assert block.markdown == r"\[value=\lceil u+v\rceil+x\]"


def test_rotated_glyphs_are_not_used_for_horizontal_repairs():
    native = evidence([("Q", 20, 100, 10, "txsys")])
    native.blocks[0]["lines"][0]["direction"] = [0, -1]
    block = Block("formula", r"\(Q\)", bbox=BOX)
    assert _restore_embedded_math_alphabets([block], native) == []


def test_tied_script_parents_abstain():
    native = evidence([("A", 20, 100, 10, "NewTXMI"),
                       ("B", 20, 100, 10, "NewTXMI"),
                       ("n", 25, 103, 7, "NewTXMI")])
    _, parents = ordered_glyphs(list(iter_embedded_characters(native)))
    assert parents == {}


def test_literal_division_is_not_rewritten_as_a_script():
    native = evidence([
        ("a=Y", 20, 100, 10, "NewTXMI"),
        ("r", 35, 103, 7, "NewTXMI"),
        ("u/v", 35, 95, 7, "NewTXMI"),
        ("+1", 48, 100, 10, "NewTXMI"),
    ])
    markdown = r"\[a=Y_{r}^{u / v}+1\]"
    alignment = align_glyphs(markdown, native, BOX, _semantic_math_projection)
    assert script_edits(markdown, alignment) == []


def test_layout_orders_centered_operator_limits_before_glyph_repair():
    native = evidence([
        ("h=", 20, 100, 10, "NewTXMI"),
        # A math-extension font has a shifted origin, but the same visual row.
        ("∑", 31, 100, 10, "txsys"),
        ("b,e", 30, 110, 7, "NewTXMI"),
        ("q", 46, 100, 10, "NewTXMI"),
        ("b,e", 51, 103, 7, "NewTXMI"),
        ("(t)", 63, 100, 10, "NewTXMI"),
    ])
    block = Block("formula", r"\[h=\sum_{b,\vec{c}}q_{b,\vec{c}}(t)\]", bbox=BOX)
    _repair_embedded_math_glyphs([block], native)
    assert block.markdown == r"\[h=\sum_{b,\vec{e}}q_{b,\vec{e}}(t)\]"


def test_proof_mark_anchors_to_text_when_ocr_box_drifts():
    native = evidence([("f(x)=a+b", 20, 100, 10, "NewTXMI"),
                       ("□", 100, 100, 10, "txsym")])
    end = Block("formula", r"\[f(x)=a+b\]", bbox=(20, 110, 90, 125))
    mark = Block("paragraph", r"\(\square\)", bbox=(100, 90, 105, 100))
    blocks = [mark, end]
    _restore_embedded_proof_marks(blocks, native, 1)
    assert blocks == [end, mark]
    assert _restore_embedded_proof_marks(blocks, native, 1) == []


def test_malformed_tex_is_not_completed_by_the_parser():
    assert tex_groups(r"Y^{a_{b}")[0].end == -1


@pytest.mark.parametrize("modifier", ["\ufe00", "\ufe04", "\ufe0f", "\U000e0100", "\u180b", " "])
def test_non_ink_modifiers_do_not_downgrade_geometry(modifier):
    native = evidence([("Visible text" + modifier, 20, 100, 10, "unknown")])
    last = native.blocks[0]["lines"][0]["spans"][0]["chars"][-1]
    last["bbox"][2] = last["bbox"][0]
    assert assess_embedded(native).geometric


@pytest.mark.parametrize("visible", ["x", "x\ufe00", "\u0301"])
def test_visible_glyphs_and_combining_accents_still_need_geometry(visible):
    native = evidence([("Visible text!", 20, 100, 10, "unknown")])
    last = native.blocks[0]["lines"][0]["spans"][0]["chars"][-1]
    last["text"] = visible
    last["bbox"][2] = last["bbox"][0]
    assert "invalid_geometry" in assess_embedded(native).reasons
    assert not assess_embedded(native).geometric


def test_selector_only_layer_is_empty_and_ignore_flag_still_wins():
    native = evidence([("\ufe00\U000e0100", 20, 100, 10, "unknown")])
    assert assess_embedded(native).state == "untrusted"
    assert assess_embedded(native).reasons == ("empty",)
    native = evidence([("Visible text", 20, 100, 10, "unknown")])
    native.extractor = "ignored"
    assert assess_embedded(native).reasons == ("disabled",)


@pytest.mark.parametrize(("opening", "closing", "left", "right"), [
    ("⌈", "⌉", r"\lceil", r"\rceil"),
    ("⌊", "⌋", r"\lfloor", r"\rfloor"),
])
def test_nested_delimiter_repair_preserves_probability_brackets(opening, closing, left, right):
    native = evidence([(f"Pr[x>{opening}(1+u)m{closing}]", 20, 100, 10, "unknown")])
    block = Block("formula", r"\[\operatorname{Pr}\left[x>\left[(1+u)m\right]\right]\]", bbox=BOX)
    expected = rf"\[\operatorname{{Pr}}\left[x>\left{left}(1+u)m\right{right}\right]\]"
    assert _repair_embedded_math_structure([block], native)
    assert block.markdown == expected
    for _ in range(3):
        assert _repair_embedded_math_glyphs([block], native) == []
        assert _repair_embedded_math_structure([block], native) == []
        assert block.markdown == expected


def test_delimiter_repair_distinguishes_neighboring_pairs():
    native = evidence([("[alpha]+⌈bravo⌉", 20, 100, 10, "unknown")])
    block = Block("formula", r"\[\left[alpha\right]+\left[bravo\right]\]", bbox=BOX)
    _repair_embedded_math_structure([block], native)
    assert block.markdown == r"\[\left[alpha\right]+\left\lceil bravo\right\rceil\]"


@pytest.mark.parametrize("text", ["⌈alpha", "alpha⌉", "⌈alpha⌋", "⌈⌉"])
def test_unpaired_or_unaligned_native_delimiters_cannot_repair(text):
    native = evidence([(text, 20, 100, 10, "unknown")])
    markdown = r"\[\left[alpha\right]\]"
    block = Block("formula", markdown, bbox=BOX)
    assert _repair_embedded_math_structure([block], native) == []
    assert block.markdown == markdown
