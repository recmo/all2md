from __future__ import annotations

import json
from pathlib import Path

import fitz
from PIL import Image

from ebook2md.chapters import chapters_from_map
from ebook2md.compare import compare_text
from ebook2md.ocr import parse_output
from ebook2md.pipeline import convert
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


def test_parse_unlimited_output():
    markdown, blocks = parse_output(
        "<|det|>title [1, 2, 3, 4]<|/det|>Hello\n<|det|>text [5,6,7,8]<|/det|>World"
    )
    assert markdown == "Hello\n\nWorld"
    assert [block.kind for block in blocks] == ["title", "text"]
    assert blocks[0].bbox == (1.0, 2.0, 3.0, 4.0)


def test_embedded_text_is_only_comparison_evidence():
    comparison = compare_text("The visual text has $x^2$.", "bad hidden layer")
    assert comparison.character_similarity < 0.9
    assert "embedded_text_low_similarity" in comparison.warnings


def test_small_token_disagreement_is_not_hidden_by_page_similarity():
    comparison = compare_text("CHAPTER I: GETTING STARTED", "CHAPTER 1: GETTING STARTED")
    assert comparison.character_similarity > 0.9
    assert comparison.disagreements == [{"operation": "replace", "visual": "I", "embedded": "1"}]
    assert "embedded_text_token_disagreement" in comparison.warnings


def test_chapter_map(tmp_path: Path):
    path = tmp_path / "chapters.json"
    path.write_text(json.dumps([{"title": "Intro", "start_page": 1}, {"title": "Next", "start_page": 3}]))
    chapters = chapters_from_map(path, [1, 2, 3, 4])
    assert [(item.start_page, item.end_page) for item in chapters] == [(1, 2), (3, 4)]


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
