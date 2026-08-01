from pathlib import Path

from ebook2md.formatting import format_markdown
from ebook2md.markdown import html_tables_to_markdown, local_links, markdown_anchors, merge_html_tables, normalize_table_blocks, strict_page_markdown, write_markdown
from ebook2md.model import Block, Chapter, Comparison, EmbeddedEvidence, PageResult


def page(number: int) -> PageResult:
    return PageResult(
        number=number,
        image=f"page-{number}.png",
        visual_markdown="Text\n\n![Figure](assets/figures/fig-0001.png)",
        blocks=[],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )


def test_chapter_links_are_relative(tmp_path: Path):
    (tmp_path / "assets/figures").mkdir(parents=True)
    (tmp_path / "assets/figures/fig-0001.png").write_bytes(b"png")
    files = write_markdown(
        tmp_path,
        [page(1), page(2)],
        [Chapter("One", 1, 1, "one"), Chapter("Two", 2, 2, "two")],
        split=True,
        title="Book",
    )
    assert files == ["book.md", "chapters/000-one.md", "chapters/001-two.md"]
    chapter = (tmp_path / "chapters/000-one.md").read_text()
    assert "../assets/figures/fig-0001.png" in chapter
    assert '<a id="page-' not in chapter
    assert "<!-- page: 1 -->" in chapter
    assert local_links(chapter) == ["../assets/figures/fig-0001.png"]
    (tmp_path / "chapters/stale.md").write_text("stale")
    write_markdown(
        tmp_path,
        [page(1), page(2)],
        [Chapter("One", 1, 1, "one"), Chapter("Two", 2, 2, "two")],
        split=True,
        title="Book",
    )
    assert not (tmp_path / "chapters/stale.md").exists()


def test_anchor_links_are_local_links():
    assert local_links("[Target](#page-4)") == ["#page-4"]


def test_html_table_becomes_pipe_table():
    source = (
        '<table><tr><td>Name</td><td>Owner</td></tr>'
        '<tr><td rowspan="2">Build</td><td>Alice</td></tr>'
        '<tr><td>Bob | Carol</td></tr></table>'
    )
    assert html_tables_to_markdown(source) == (
        "| Name | Owner |\n"
        "| --- | --- |\n"
        "| Build | Alice |\n"
        r"| Build | Bob \| Carol |"
    )


def test_matching_table_continuations_merge_without_repeated_header():
    first = "<table><tr><td>A</td><td></td><td>B</td></tr><tr><td>1</td><td>2</td><td></td></tr></table>"
    second = "<table><tr><td>A</td><td>B</td></tr><tr><td>3</td><td>4</td></tr></table>"
    merged = merge_html_tables(first, second)
    assert merged is not None
    markdown = html_tables_to_markdown(merged)
    assert markdown.count("| A | B |") == 1
    assert "| 1 | 2 |" in markdown
    assert "| 3 | 4 |" in markdown


def test_adjacent_boundary_tables_merge_without_repeated_header():
    first = "<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>"
    second = "<table><tr><td>3</td><td>4</td></tr></table>"
    merged = merge_html_tables(first, second, adjacent=True, boundary_geometry=True)
    assert merged is not None
    assert "| 3 | 4 |" in html_tables_to_markdown(merged)


def test_complex_table_records_html_fallback_reason():
    block = Block(
        "table",
        '<table><tr><td colspan="2">Spanning header</td></tr><tr><td>A</td><td>B</td></tr></table>',
    )
    normalize_table_blocks([block])
    assert block.markdown.startswith("<table")
    assert block.metadata["html_fallback_reason"] == "column_span"


def test_formatter_is_gfm_idempotent_and_preserves_evidence_syntax():
    source = (
        "# Title\n\n<!-- page: 93 -->\n\n"
        "| A | B |\n|---|---|\n| $x^2$ | line\\\nbreak |\n\n"
        "![Diagram](assets/figures/diagram.png)\n\n*Figure 1. Diagram.*\n"
    )
    once = format_markdown(source)
    assert format_markdown(once) == once
    assert "<!-- page: 93 -->" in once
    assert "$x^2$" in once
    assert "assets/figures/diagram.png" in once


def test_outline_and_visual_titles_become_markdown_hierarchy():
    result = PageResult(
        number=10,
        image="page.png",
        visual_markdown="",
        blocks=[
            Block("title", "CHAPTER I: START"),
            Block("text", "Body"),
            Block("title", "A SECTION"),
            Block("table", "<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>"),
        ],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )
    markdown = strict_page_markdown(
        result,
        [{"level": 2, "title": "Chapter 1: Start", "page": 10}],
    )
    assert markdown.startswith("## Chapter 1: Start\n\nBody")
    assert "### A SECTION" in markdown
    assert "<table" not in markdown
    assert "| A | B |" in markdown


def test_markdown_headings_are_valid_link_anchors():
    assert markdown_anchors("# Part I: Beginning\n\n## Chapter 1: Start") == {
        "part-i-beginning",
        "chapter-1-start",
    }


def test_single_file_accepts_multiple_structural_boundaries(tmp_path: Path):
    files = write_markdown(
        tmp_path,
        [page(1), page(2)],
        [Chapter("One", 1, 1, "one"), Chapter("Two", 2, 2, "two")],
        split=False,
        title="Book",
    )
    assert files == ["book.md"]


def test_synthetic_matter_file_contains_level_two_sections(tmp_path: Path):
    first = page(1)
    first.visual_markdown = "# Introduction\n\nText"
    write_markdown(
        tmp_path,
        [first],
        [Chapter("Front matter", 1, 1, "front-matter")],
        split=True,
        title="Book",
    )
    markdown = (tmp_path / "chapters/000-front-matter.md").read_text()
    assert markdown.startswith("# Front matter")
    assert "## Introduction" in markdown
