from __future__ import annotations

from copy import deepcopy
import json

import fitz
import pytest

from pages2md.formatting import format_and_lint, format_markdown
from pages2md.lists import normalize_lists, parse_marker, validate_list_node
from pages2md.markdown import page_markdown, strict_page_markdown
from pages2md.model import Block, Comparison, EmbeddedEvidence, PageResult
from pages2md.native import parse_native_observation
from pages2md.pipeline import _normalize_document_blocks, convert
from pages2md.verify import verify_bundle


def page(number: int, blocks: list[Block]) -> PageResult:
    return PageResult(
        number=number,
        image=f"page-{number}.png",
        visual_markdown="",
        blocks=blocks,
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )


def item(value: str, top: float, left: float = 100) -> Block:
    return Block("paragraph", value, (left, top, 800, top + 24), source_pages=[1])


def list_node(result: PageResult) -> dict:
    assert len(result.blocks) == 1
    assert result.blocks[0].kind == "list"
    return result.blocks[0].metadata["list"]


def test_list_grouping_preserves_embedded_repair_audit():
    block = item(r"1. Use \(x\).", 100)
    repair = {"kind": "inline_math", "visual": "x", "embedded": r"\(x\)"}
    block.metadata["embedded_text_repairs"] = [repair]
    result = page(1, [block])
    normalize_lists([result])
    assert result.blocks[0].metadata["embedded_text_repairs"] == [repair]


def test_mixed_visual_and_markdown_bullets_become_one_compact_list():
    result = page(1, [
        item("• Zone of Incompetence", 100),
        item("• Zone of Competence", 130),
        item("- Zone of Excellence", 160),
        item("- Zone of Genius", 190),
    ])

    _normalize_document_blocks([result])

    assert result.blocks[0].markdown == (
        "- Zone of Incompetence\n"
        "- Zone of Competence\n"
        "- Zone of Excellence\n"
        "- Zone of Genius"
    )
    node = list_node(result)
    assert node["ordered"] is False
    assert [entry["source_marker"] for entry in node["items"]] == ["•", "•", "-", "-"]
    assert all(entry["source_pages"] == [1] for entry in node["items"])


def test_all_supported_visual_bullets_are_normalized():
    markers = ["•", "◦", "▪", "‣", "●", "○", "–", "—", "-", "*", "+"]
    result = page(1, [Block(
        "list",
        "\n".join(f"{marker} Item {index}" for index, marker in enumerate(markers, 1)),
        (100, 100, 800, 500),
        source_pages=[1],
    )])

    normalize_lists([result])

    node = list_node(result)
    assert [entry["source_marker"] for entry in node["items"]] == markers
    assert result.blocks[0].markdown.count("\n") == len(markers) - 1
    assert all(line.startswith("- Item") for line in result.blocks[0].markdown.splitlines())


def test_decimal_marker_variants_render_explicit_ordinals():
    result = page(1, [item("1. First", 100), item("2) Second", 130), item("(3) Third", 160)])

    normalize_lists([result])

    node = list_node(result)
    assert node["ordered"] is True
    assert node["marker_style"] == "decimal"
    assert [entry["source_marker"] for entry in node["items"]] == ["1.", "2)", "(3)"]
    assert [entry["source_ordinal"] for entry in node["items"]] == [1, 2, 3]
    assert result.blocks[0].markdown == "1. First\n2. Second\n3. Third"
    assert format_markdown(result.blocks[0].markdown) == "1. First\n2. Second\n3. Third\n"


def test_wide_decimal_markers_indent_continuations_for_gfm():
    result = page(1, [Block(
        "list",
        "100. First paragraph\n\n  Continued paragraph.\n101. Next",
        (100, 100, 800, 220),
        source_pages=[1],
    )])

    normalize_lists([result])

    assert "\n     Continued paragraph." in result.blocks[0].markdown
    assert format_markdown(format_markdown(result.blocks[0].markdown)) == format_markdown(result.blocks[0].markdown)


def test_alphabetic_labels_preserve_case_and_delimiters():
    lower = page(1, [item("(a) First", 100), item("b) Second", 130), item("c. Third", 160)])
    upper = page(2, [
        Block("paragraph", "(A) First", (100, 100, 800, 124), source_pages=[2]),
        Block("paragraph", "B) Second", (100, 130, 800, 154), source_pages=[2]),
    ])

    normalize_lists([lower, upper])

    assert lower.blocks[0].markdown == "- **(a)** First\n- **b)** Second\n- **c.** Third"
    assert upper.blocks[0].markdown == "- **(A)** First\n- **B)** Second"
    assert list_node(lower)["marker_style"] == "alpha"
    assert list_node(upper)["marker_case"] == "upper"


def test_roman_labels_preserve_case_and_references_remain_readable():
    result = page(1, [
        Block("paragraph", "Repeat step (ii) if needed.", (80, 50, 800, 75), source_pages=[1]),
        item("(i) First phase", 100),
        item("(ii) Second phase", 130),
        item("(iii) Third phase", 160),
    ])

    normalize_lists([result])

    assert result.blocks[0].kind == "paragraph"
    assert "step (ii)" in result.blocks[0].markdown
    node = result.blocks[1].metadata["list"]
    assert node["marker_style"] == "roman"
    assert result.blocks[1].markdown == (
        "- **(i)** First phase\n- **(ii)** Second phase\n- **(iii)** Third phase"
    )


def test_uppercase_roman_scheme_is_recognized():
    result = page(1, [item("I. First", 100), item("II. Second", 130), item("III. Third", 160)])

    normalize_lists([result])

    node = list_node(result)
    assert node["marker_style"] == "roman"
    assert node["marker_case"] == "upper"
    assert result.blocks[0].markdown.startswith("- **I.** First")


def test_multiline_and_multi_paragraph_items_render_as_loose_only_when_needed():
    result = page(1, [Block(
        "list",
        "- First line\n  continues here\n\n  A second paragraph.\n- Second item",
        (100, 100, 800, 240),
        source_pages=[1],
    )])

    normalize_lists([result])

    assert result.blocks[0].markdown == (
        "- First line continues here\n\n"
        "    A second paragraph.\n"
        "- Second item"
    )
    assert len(list_node(result)["items"][0]["blocks"]) == 2


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "- Method 2 invites the team\n\n  to give feedback and determines the final answer.\n- Next method",
            "- Method 2 invites the team to give feedback and determines the final answer.\n- Next method",
        ),
        (
            "a. The goal is to avoid acrimony in the relationship\n\n  again. Person A goes first.\nb. Write down the plan.",
            "- **a.** The goal is to avoid acrimony in the relationship again. Person A goes first.\n- **b.** Write down the plan.",
        ),
    ],
)
def test_mid_sentence_list_fragments_render_as_compact_items(source, expected):
    result = page(1, [Block("list", source, (100, 100, 800, 300), source_pages=[1])])

    normalize_lists([result])

    assert result.blocks[0].markdown == expected
    assert list_node(result)["continuation_repairs"] == 1


def test_page_leading_fragment_is_reattached_after_nested_item_head():
    nested = {
        "ordered": True,
        "marker_style": "alpha",
        "marker_case": "lower",
        "items": [{
            "source_marker": "a.",
            "source_label": "a",
            "source_ordinal": 1,
            "blocks": [{"kind": "paragraph", "markdown": "If the answer is yes, that person will create toxic"}],
            "children": [],
        }],
    }
    node = {
        "ordered": True,
        "marker_style": "decimal",
        "marker_case": None,
        "items": [
            {
                "source_marker": "1.",
                "source_label": "1",
                "source_ordinal": 1,
                "blocks": [{"kind": "paragraph", "markdown": "Facilitator asks the question."}],
                "children": [],
            },
            {
                "source_marker": "2.",
                "source_label": "2",
                "source_ordinal": 2,
                "blocks": [
                    {"kind": "paragraph", "markdown": 'Person B: "No."'},
                    {"kind": "paragraph", "markdown": "relationships with others and eventually leave the company anyway."},
                ],
                "children": [nested],
            },
        ],
    }
    result = page(76, [Block(
        "list",
        "stale rendering",
        (147, 699, 825, 763),
        metadata={"list": node},
        source_pages=[76, 77],
    )])

    normalize_lists([result])

    assert result.blocks[0].markdown == (
        "1. Facilitator asks the question.\n"
        '2. Person B: "No."\n'
        "    - **a.** If the answer is yes, that person will create toxic relationships with others and eventually leave the company anyway."
    )
    assert len(node["items"][1]["blocks"]) == 1
    assert nested["items"][0]["blocks"][0]["markdown"].endswith("company anyway.")
    assert validate_list_node(node) == []


def test_adjacent_normalized_list_blocks_preserve_numeric_to_alpha_nesting():
    numeric = page(77, [Block(
        "list", "5. Write down the action item.", (145, 135, 850, 262), source_pages=[77]
    )])
    alpha = page(77, [Block(
        "list",
        "a. Cocreate a plan.\nb. Write down the agreement.",
        (165, 265, 848, 459),
        source_pages=[77],
    )])
    normalize_lists([numeric])
    normalize_lists([alpha])
    result = page(77, [numeric.blocks[0], alpha.blocks[0]])

    normalize_lists([result])

    assert len(result.blocks) == 1
    assert result.blocks[0].markdown == (
        "5. Write down the action item.\n"
        "    - **a.** Cocreate a plan.\n"
        "    - **b.** Write down the agreement."
    )
    node = list_node(result)
    assert node["items"][0]["children"][0]["nesting_level"] == 1
    assert result.blocks[0].metadata["stitched_list_blocks"] == 1


def test_cross_page_child_list_keeps_active_indent_and_page_comment():
    first = page(53, [Block(
        "list",
        "2. The R schedules a decision meeting.\n    a. If urgent, schedule it immediately.",
        (145, 700, 850, 900),
        source_pages=[53],
    )])
    second = page(54, [Block(
        "list",
        "b. If it is not urgent, use the next team meeting.",
        (165, 91, 782, 177),
        source_pages=[54],
    )])

    normalize_lists([first, second])
    second.visual_markdown = strict_page_markdown(second, [])
    rendered = page_markdown(second, chapter=False)

    left_child = list_node(first)["items"][-1]["children"][-1]
    right = list_node(second)
    assert left_child["continues_on_page"] == 54
    assert right["continues_from_page"] == 53
    assert second.blocks[0].metadata["render_indent"] == 4
    assert rendered.startswith(
        "    <!-- page: 54 -->\n\n"
        "    - **b.** If it is not urgent, use the next team meeting."
    )


def test_nested_lists_are_inferred_from_geometry():
    result = page(1, [
        item("• Parent", 100, 100),
        item("◦ Child one", 130, 145),
        item("◦ Child two", 160, 145),
        item("• Next parent", 190, 100),
    ])

    normalize_lists([result])

    node = list_node(result)
    assert len(node["items"]) == 2
    child = node["items"][0]["children"][0]
    assert [entry["blocks"][0]["markdown"] for entry in child["items"]] == ["Child one", "Child two"]
    assert result.blocks[0].markdown == "- Parent\n    - Child one\n    - Child two\n- Next parent"


def test_cross_page_decimal_continuity_is_recorded_without_renumbering():
    first = page(1, [item("1) First", 100), item("2) Second", 130)])
    second = page(2, [
        Block("paragraph", "3) Third", (100, 100, 800, 124), source_pages=[2]),
        Block("paragraph", "4) Fourth", (100, 130, 800, 154), source_pages=[2]),
    ])

    normalize_lists([first, second])

    assert list_node(first)["continues_on_page"] == 2
    assert list_node(second)["continues_from_page"] == 1
    assert second.blocks[0].markdown == "3. Third\n4. Fourth"


def test_item_continuation_can_cross_a_page_when_geometry_supports_it():
    first = page(1, [
        Block("list", "- An item that continues", (100, 850, 800, 900), source_pages=[1])
    ])
    second = page(2, [
        Block("paragraph", "onto the following page.", (130, 80, 800, 120), source_pages=[2])
    ])

    normalize_lists([first, second])

    assert not second.blocks
    node = list_node(first)
    assert node["items"][0]["source_pages"] == [1, 2]
    assert first.blocks[0].markdown == "- An item that continues onto the following page."
    assert len(node["items"][0]["blocks"]) == 1


def test_headings_prose_tables_and_figures_interrupt_but_preserve_visual_list_items():
    for barrier in (
        Block("heading", "Section", (80, 130, 800, 155)),
        Block("paragraph", "Unrelated prose.", (80, 130, 800, 155)),
        Block("table", "| A |\n| --- |\n| B |", (80, 130, 800, 200)),
        Block("figure", "Diagram", (80, 130, 800, 300)),
    ):
        result = page(1, [item("• First", 100), barrier, item("• Second", 330)])
        normalize_lists([result])
        assert [block.kind for block in result.blocks] == ["list", barrier.kind, "list"]


def test_false_positive_shapes_remain_prose():
    values = [
        "2019 was a good year.",
        "3.14 is approximately pi.",
        "(note) This is parenthetical prose.",
        "A sentence — with an em dash.",
        "IV. is a Roman-looking fragment.",
        "- A single dash-led line without list evidence.",
    ]
    result = page(1, [Block("paragraph", value) for value in values])

    normalize_lists([result])

    assert [block.kind for block in result.blocks] == ["paragraph"] * len(values)
    assert [block.markdown for block in result.blocks] == values


def test_single_visual_bullet_is_normalized_without_contextual_evidence():
    result = page(1, [item("• One day of internal meetings", 100)])

    normalize_lists([result])

    assert result.blocks[0].kind == "list"
    assert result.blocks[0].markdown == "- One day of internal meetings"
    assert list_node(result)["items"][0]["source_marker"] == "•"


def test_empty_native_list_container_is_discarded():
    result = page(1, [Block(
        "list",
        "",
        (100, 100, 800, 200),
        metadata={"list_container": True},
        source_pages=[1],
    )])

    normalize_lists([result])

    assert result.blocks == []


def test_standalone_visual_markers_without_item_text_are_discarded():
    result = page(1, [Block(
        "paragraph",
        "•\n•\n•",
        (100, 100, 800, 200),
        source_pages=[1],
    )])

    normalize_lists([result])

    assert result.blocks == []


def test_native_parser_retains_list_container_and_candidates():
    raw = (
        "<|det|>list [100,100,800,200]<|/det|>\n"
        "<|det|>text [120,110,700,130]<|/det|>• First\n"
        "<|det|>text [120,140,700,160]<|/det|>- Second"
    )

    observation = parse_native_observation(raw, mode="multi_base", source_pages=[7])

    assert observation.blocks[0].kind == "list"
    assert observation.blocks[0].metadata["list_container"] is True
    assert observation.blocks[1].metadata["list_candidates"][0]["source_marker"] == "•"
    assert observation.blocks[2].metadata["list_candidates"][0]["source_marker"] == "-"


def test_list_rendering_and_reassembly_are_byte_stable():
    result = page(1, [item("(a) First", 100), item("(b) Second", 130)])
    normalize_lists([result])
    first = result.blocks[0].markdown
    snapshot = deepcopy(result.blocks[0].metadata["list"])

    normalize_lists([result])
    formatted = format_markdown(first)

    assert result.blocks[0].metadata["list"] == snapshot
    assert result.blocks[0].markdown == first
    assert format_markdown(formatted) == formatted
    assert validate_list_node(snapshot) == []


def test_list_formatter_and_linter_preserve_numbering_labels_and_nesting(tmp_path):
    pytest.importorskip("pymarkdown")
    path = tmp_path / "lists.md"
    path.write_text(
        "# Lists\n\n"
        "1. First\n2. Second\n3. Third\n\n"
        "- **(a)** Condition\n"
        "    - Nested item\n"
        "- **(b)** Next condition\n"
    )

    result = format_and_lint([path])
    rendered = path.read_text()

    assert result.idempotent is True
    assert result.lint_errors == []
    assert "1. First\n2. Second\n3. Third" in rendered
    assert "**(a)**" in rendered and "**(b)**" in rendered
    assert "  - Nested item" in rendered
    assert format_markdown(rendered) == rendered


def test_marker_parser_rejects_embedded_or_word_like_markers():
    assert parse_marker("Text - not a list") is None
    assert parse_marker("Proof. This is prose") is None
    assert parse_marker("3.14 is a decimal") is None
    assert parse_marker("(a) A condition") is not None


def test_equations_and_paragraphs_stay_inside_their_enclosing_list_item(tmp_path):
    first_math = "\\[\n\\begin{aligned}\nx &= y \\\\\n  &= z\n\\end{aligned}\n\\]"
    second_math = r"\[f(x)=0\]"
    result = page(1, [
        item("1. Construct an object,", 100),
        Block("formula", first_math, (200, 130, 600, 170), source_pages=[1]),
        item("Consequently, the object satisfies", 180, left=120),
        Block("formula", second_math, (200, 210, 600, 230), source_pages=[1]),
        item("This finishes the construction.", 240, left=120),
        item("2. Recover the answer.", 270),
        item("The following discussion is outside the list.", 310, left=80),
    ])
    normalize_lists([result])
    assert len(result.blocks) == 2
    node = result.blocks[0].metadata["list"]
    assert [i["source_ordinal"] for i in node["items"]] == [1, 2]
    body = node["items"][0]["blocks"]
    assert [b["kind"] for b in body] == ["paragraph", "formula", "paragraph", "formula", "paragraph"]
    assert body[1]["markdown"] == first_math
    assert body[3]["markdown"] == second_math
    assert body[1]["bbox"] == [200, 130, 600, 170]
    assert node["items"][0]["bbox"] == [100, 100, 800, 264]
    assert not validate_list_node(node)
    before = deepcopy(result.blocks)
    normalize_lists([result])
    assert result.blocks == before
    path = tmp_path / "book.md"
    path.write_text("# Results\n\n" + strict_page_markdown(result, []) + "\n")
    lint = format_and_lint([path])
    assert not lint.lint_errors
    if lint.math_validation.status == "checked":
        assert lint.math_validation.checked == 2
        assert not lint.math_validation.diagnostics


@pytest.mark.parametrize("change", ["no_sibling", "heading", "outdent", "wrong_ordinal", "other_margin", "missing_geometry"])
def test_formula_does_not_extend_a_list_without_enclosing_structural_evidence(change):
    blocks = [item("1. First item.", 100),
              Block("formula", r"\[x=y\]", (200, 130, 600, 150)),
              item("An explanation.", 160, left=120),
              item("2. Second item.", 190)]
    if change == "no_sibling": blocks.pop()
    elif change == "heading": blocks[2].kind = "heading"
    elif change == "outdent": blocks[2].bbox = (80, 160, 800, 184)
    elif change == "wrong_ordinal": blocks[-1].markdown = "4. A different sequence."
    elif change == "other_margin": blocks[-1].bbox = (200, 190, 800, 214)
    elif change == "missing_geometry": blocks[1].bbox = None
    result = page(1, blocks)
    normalize_lists([result])
    assert any(block.kind == "formula" for block in result.blocks)


@pytest.mark.parametrize(("first", "second"), [("100.", "101."), ("(a)", "(b)"), ("(i)", "(ii)"), ("•", "•")])
def test_enclosed_math_is_not_specific_to_decimal_two_item_lists(first, second):
    result = page(1, [item(f"{first} First.", 100),
                      Block("formula", r"\[a=b\]", (200, 130, 600, 150)),
                      item(f"{second} Second.", 160)])
    normalize_lists([result])
    node = list_node(result)
    assert node["items"][0]["blocks"][1]["kind"] == "formula"
    assert len(node["items"]) == 2


def test_list_output_is_stable_without_publishing_intermediates(tmp_path):
    class ListOcr:
        identity = {"engine": "fixture-list", "model": "fixture", "revision": "1"}

        def __init__(self):
            self.calls = 0

        def recognize(self, image):
            self.calls += 1
            return (
                "<|det|>list [100,180,800,300]<|/det|>\n"
                "<|det|>text [120,190,700,215]<|/det|>• First\n"
                "<|det|>text [120,220,700,245]<|/det|>- Second\n"
                "<|det|>text [120,250,700,275]<|/det|>◦ Third",
                {"finish_reason": "stop"},
            )

    source = tmp_path / "fixture.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "auxiliary text")
    document.save(source)
    document.close()
    backend = ListOcr()

    bundle = convert(source, backend=backend)
    first = bundle.read_bytes()

    with pytest.raises(FileExistsError, match="--force"):
        convert(source, backend=backend)
    repeated = convert(source, force=True, backend=backend)

    assert backend.calls == 1
    assert repeated.read_bytes() == first
    assert "•" not in first.decode()
    assert bundle == tmp_path / "fixture.md"
    assert verify_bundle(repeated).ok
