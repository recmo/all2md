from __future__ import annotations

from copy import deepcopy
import json

import fitz
import pytest

from ebook2md.formatting import format_and_lint, format_markdown
from ebook2md.lists import normalize_lists, parse_marker, validate_list_node
from ebook2md.model import Block, Comparison, EmbeddedEvidence, PageResult
from ebook2md.native import parse_native_observation
from ebook2md.pipeline import _normalize_document_blocks, convert
from ebook2md.verify import verify_bundle


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
    assert first.blocks[0].markdown == "- An item that continues\n\n    onto the following page."


def test_headings_prose_tables_and_figures_interrupt_list_grouping():
    for barrier in (
        Block("heading", "Section", (80, 130, 800, 155)),
        Block("paragraph", "Unrelated prose.", (80, 130, 800, 155)),
        Block("table", "| A |\n| --- |\n| B |", (80, 130, 800, 200)),
        Block("figure", "Diagram", (80, 130, 800, 300)),
    ):
        result = page(1, [item("• First", 100), barrier, item("• Second", 330)])
        normalize_lists([result])
        assert [block.kind for block in result.blocks] == ["paragraph", barrier.kind, "paragraph"]


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


def test_list_bundle_verification_and_interrupted_resume_are_stable(tmp_path):
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

    bundle = convert(source, tmp_path / "out", backend=backend, split_mode="single", quality="fast")
    first = (bundle / "book.md").read_bytes()
    page_json = json.loads((bundle / "pages/page-0001.json").read_text())
    metadata = json.loads((bundle / "metadata.json").read_text())
    metadata["failed_pages"] = [{"page": 2, "error": "simulated interruption"}]
    (bundle / "metadata.json").write_text(json.dumps(metadata))

    resumed = convert(source, tmp_path / "out", backend=backend, split_mode="single", quality="fast")

    assert backend.calls == 1
    assert (resumed / "book.md").read_bytes() == first
    assert page_json["blocks"][0]["metadata"]["list"]["items"][0]["source_marker"] == "•"
    assert "•" not in first.decode()
    assert verify_bundle(resumed).ok
