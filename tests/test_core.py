from __future__ import annotations

import json
from pathlib import Path

import fitz
from PIL import Image

from ebook2md.chapters import chapters_from_map
from ebook2md.chapters import detect_chapters
from ebook2md.compare import compare_text
from ebook2md.ocr import parse_output, split_multi_page_output
from ebook2md.pipeline import _align_multi_results, _merge_continued_tables, convert
from ebook2md.model import Block, Comparison, EmbeddedEvidence, PageResult, SourceDocument, SourcePage
from ebook2md.verify import verify_bundle


class FixtureOcr:
    identity = {"engine": "fixture", "model": "fixture", "revision": "1"}

    def recognize(self, image: Path):
        return (
            "<|det|>title [100, 80, 900, 150]<|/det|>Chapter One\n"
            "<|det|>text [100, 180, 900, 500]<|/det|>The visual text has $x^2$.\n"
            "<|det|>diagram [200, 550, 800, 900]<|/det|>A useful diagram.",
            {"finish_reason": "stop"},
        )


class TableFixtureOcr:
    identity = {"engine": "fixture", "model": "fixture", "revision": "1"}

    def recognize(self, image: Path):
        return (
            "<|det|>table [150, 350, 420, 600]<|/det|>"
            "<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>",
            {},
        )


def test_parse_unlimited_output():
    markdown, blocks = parse_output(
        "<|det|>title [1, 2, 3, 4]<|/det|>Hello\n<|det|>text [5,6,7,8]<|/det|>World"
    )
    assert markdown == "Hello\n\nWorld"
    assert [block.kind for block in blocks] == ["title", "text"]
    assert blocks[0].bbox == (1.0, 2.0, 3.0, 4.0)


def test_split_multi_page_output_requires_one_segment_per_image():
    assert split_multi_page_output("<PAGE>\nOne\n<PAGE>\nTwo", 2) == ["One", "Two"]
    assert split_multi_page_output("<PAGE>\nMerged", 2) == ["Merged"]


def test_multi_page_segments_align_to_physical_page_ranges(tmp_path: Path):
    pages = [
        SourcePage(1, tmp_path / "1.png", EmbeddedEvidence(text="Alpha")),
        SourcePage(2, tmp_path / "2.png", EmbeddedEvidence(text="Beta")),
        SourcePage(3, tmp_path / "3.png", EmbeddedEvidence(text="Gamma")),
    ]
    aligned = _align_multi_results(
        pages,
        [("Alpha", {}), ("Beta Gamma", {})],
    )
    assert [item[1]["source_pages"] for item in aligned] == [[1], [2, 3], [2, 3]]
    assert aligned[2][0] == ""
    assert aligned[2][1]["merged_into"] == 2


def test_multi_page_continuation_assets_are_preserved_but_not_rendered():
    first = PageResult(
        number=1,
        image="",
        visual_markdown="",
        blocks=[Block(kind="table", markdown="<table><tr><td>A</td></tr></table>")],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )
    continuation = PageResult(
        number=2,
        image="",
        visual_markdown="![Embedded figure](assets/figures/table-slice.png)",
        blocks=[
            Block(
                kind="embedded_figure",
                markdown="![Embedded figure](assets/figures/table-slice.png)",
                asset_id="fig-1",
            )
        ],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
        source_assets=[{"asset_id": "fig-1"}],
    )
    _merge_continued_tables([first, continuation])
    assert continuation.blocks == []
    assert continuation.visual_markdown == ""
    assert continuation.source_assets == [{"asset_id": "fig-1"}]


def test_embedded_text_is_only_comparison_evidence():
    comparison = compare_text("The visual text has $x^2$.", "bad hidden layer")
    assert comparison.character_similarity < 0.9
    assert "embedded_text_low_similarity" in comparison.warnings


def test_small_token_disagreement_is_not_hidden_by_page_similarity():
    comparison = compare_text("CHAPTER I: GETTING STARTED", "CHAPTER 1: GETTING STARTED")
    assert comparison.character_similarity > 0.9
    assert comparison.disagreements == [{"operation": "replace", "visual": "I", "embedded": "1"}]
    assert "embedded_text_token_disagreement" in comparison.warnings


def test_short_repetition_loop_is_flagged():
    comparison = compare_text("2. 2. 2. 2. 2. 2. 2. 2.", "A normal numbered list")
    assert "visual_text_repetition" in comparison.warnings


def test_repeated_table_cells_are_not_treated_as_generation_loop():
    comparison = compare_text(
        "<table><tr><td>Status</td></tr><tr><td>ON TRACK</td></tr>"
        "<tr><td>ON TRACK</td></tr><tr><td>ON TRACK</td></tr></table>",
        "",
    )
    assert "visual_text_repetition" not in comparison.warnings


def test_chapter_map(tmp_path: Path):
    path = tmp_path / "chapters.json"
    path.write_text(json.dumps([{"title": "Intro", "start_page": 1}, {"title": "Next", "start_page": 3}]))
    chapters = chapters_from_map(path, [1, 2, 3, 4])
    assert [(item.start_page, item.end_page) for item in chapters] == [(1, 2), (3, 4)]


def test_outline_splits_at_chapter_level_and_keeps_parts(tmp_path: Path):
    source = SourceDocument(
        path=tmp_path / "book.pdf",
        kind="pdf",
        outline=[
            {"level": 1, "title": "Part I", "page": 1},
            {"level": 2, "title": "Chapter 1: Start", "page": 2},
            {"level": 3, "title": "A section", "page": 3},
            {"level": 2, "title": "Chapter 2: Next", "page": 4},
            {"level": 1, "title": "Conclusion", "page": 6},
        ],
    )
    pages = [
        PageResult(number=number, image="", visual_markdown="", blocks=[], embedded=EmbeddedEvidence(), comparison=Comparison())
        for number in range(1, 7)
    ]
    chapters = detect_chapters(source, pages)
    assert [chapter.title for chapter in chapters] == ["Part I", "Chapter 1: Start", "Chapter 2: Next", "Conclusion"]
    assert [chapter.start_page for chapter in chapters] == [1, 2, 4, 6]


def test_many_short_chapters_use_parts_as_file_units(tmp_path: Path):
    outline = [{"level": 1, "title": "Introduction", "page": 1}]
    page = 2
    for part in range(1, 4):
        outline.append({"level": 1, "title": f"Part {part}", "page": page})
        for chapter in range(1, 5):
            page += 2
            outline.append({"level": 2, "title": f"Chapter {part}-{chapter}", "page": page})
    page += 2
    outline.append({"level": 1, "title": "Acknowledgments", "page": page})
    pages = [
        PageResult(number=number, image="", visual_markdown="", blocks=[], embedded=EmbeddedEvidence(), comparison=Comparison())
        for number in range(1, page + 3)
    ]
    source = SourceDocument(path=tmp_path / "book.pdf", kind="pdf", outline=outline)
    chapters = detect_chapters(source, pages)
    assert [chapter.title for chapter in chapters] == ["Front matter", "Part 1", "Part 2", "Part 3", "Back matter"]


def test_pdf_bundle_assets_links_and_evidence(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (100, 100), "navy").save(image_path)
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "broken embedded text")
    page.insert_image(fitz.Rect(100, 300, 250, 450), filename=str(image_path))
    document.save(pdf)
    document.close()

    bundle = convert(pdf, tmp_path / "out", backend=FixtureOcr(), split_mode="single")
    markdown = (bundle / "book.md").read_text()
    assert "The visual text has $x^2$." in markdown
    assert "broken embedded text" not in markdown
    assert "![A useful diagram.]" in markdown
    assert "![Embedded figure]" in markdown
    page_json = json.loads((bundle / "pages/page-0001.json").read_text())
    assert "broken embedded text" in page_json["embedded"]["text"]
    assert "<|det|>diagram" in page_json["raw_ocr"]
    assert (bundle / "conversion.log").exists()
    manifest = json.loads((bundle / "assets/manifest.json").read_text())
    assert len(manifest["assets"]) >= 2
    verification = verify_bundle(bundle)
    assert verification.ok, verification.errors

    resumed = convert(pdf, tmp_path / "out", backend=FixtureOcr(), split_mode="single")
    resumed_manifest = json.loads((resumed / "assets/manifest.json").read_text())
    assert len(resumed_manifest["assets"]) == len(manifest["assets"])


def test_embedded_table_image_is_preserved_but_not_displayed_twice(tmp_path: Path):
    pdf = tmp_path / "table.pdf"
    image_path = tmp_path / "table.png"
    Image.new("RGB", (200, 200), "white").save(image_path)
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(92, 277, 257, 475), filename=str(image_path))
    document.save(pdf)
    document.close()
    bundle = convert(pdf, tmp_path / "out", backend=TableFixtureOcr(), split_mode="single")
    markdown = (bundle / "book.md").read_text()
    assert "| A | B |" in markdown
    assert "![Embedded figure]" not in markdown
    manifest = json.loads((bundle / "assets/manifest.json").read_text())
    assert manifest["assets"]
