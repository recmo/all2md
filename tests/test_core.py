from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import fitz
from PIL import Image

from ebook2md.chapters import chapters_from_map
from ebook2md.chapters import detect_chapters
from ebook2md.compare import compare_text
from ebook2md.ocr import GUNDAM_PROMPT, MULTI_PAGE_PROMPT, MlxUnlimitedOcr, parse_output, split_multi_page_output
from ebook2md.native import parse_native_observation, reconcile_observations
from ebook2md.pipeline import _align_multi_results, _merge_continued_tables, _normalize_document_blocks, convert
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


class RecoveryFixtureOcr:
    identity = {"engine": "fixture", "model": "fixture", "revision": "1"}

    def recognize_pages(self, images: list[Path]):
        return (
            "<|det|>text [100,100,800,300]<|/det|>A truncated local sentence",
            {"mode": "multi_base", "finish_reason": "length"},
        )

    def recognize(self, image: Path):
        return (
            "<|det|>text [100,100,800,300]<|/det|>A truncated local sentence with recovered detail.",
            {"mode": "gundam", "finish_reason": "stop"},
        )


class InterruptingFixtureOcr:
    identity = {"engine": "fixture", "model": "fixture", "revision": "1"}

    def __init__(self):
        self.fail_page_two = True
        self.calls: dict[int, int] = {}

    def recognize_pages(self, images: list[Path]):
        number = int(images[0].stem.rsplit("-", 1)[-1])
        self.calls[number] = self.calls.get(number, 0) + 1
        if number == 2 and self.fail_page_two:
            self.fail_page_two = False
            raise RuntimeError("simulated interruption")
        return (
            f"<|det|>text [100,100,800,300]<|/det|>Page {number} content.",
            {"mode": "multi_base", "finish_reason": "stop"},
        )

    def recognize(self, image: Path):
        raise AssertionError("Gundam recovery is not expected for matching fixture text")


def test_parse_unlimited_output():
    markdown, blocks = parse_output(
        "<|det|>title [1, 2, 3, 4]<|/det|>Hello\n<|det|>text [5,6,7,8]<|/det|>World"
    )
    assert markdown == "Hello\n\nWorld"
    assert [block.kind for block in blocks] == ["title", "text"]
    assert blocks[0].bbox == (1.0, 2.0, 3.0, 4.0)


def test_native_stream_parser_preserves_grounding_pages_and_tables():
    raw = (Path(__file__).parent / "fixtures/ceo_failure_shapes.txt").read_text()
    observation = parse_native_observation(
        raw,
        mode="multi_base",
        source_pages=[12, 13],
        generation={"finish_reason": "stop"},
    )
    assert observation.raw == raw
    assert [block.kind for block in observation.blocks] == [
        "heading",
        "heading",
        "paragraph",
        "table",
        "table",
        "paragraph",
    ]
    assert observation.blocks[0].bbox == (110.0, 80.0, 890.0, 145.0)
    assert observation.blocks[0].source_pages == [12]
    assert observation.blocks[-1].source_pages == [13]
    assert not observation.warnings


def test_native_stream_flags_malformed_grounding_and_coordinates():
    observation = parse_native_observation(
        "<|ref|>text<|/ref|><|det|>[[900,20,100,40]]<|/det|>Broken<|det|>",
        mode="multi_base",
        source_pages=[1],
    )
    assert "visual_malformed_grounding" in observation.warnings
    assert "visual_implausible_coordinates" in observation.warnings


def test_gundam_reconciliation_is_local_and_never_replaces_tables():
    primary = parse_native_observation(
        "<|det|>text [10,10,500,100]<|/det|>A short sentnce."
        "<|det|>table [10,200,900,900]<|/det|><table><tr><td>A</td></tr></table>",
        mode="multi_base",
        source_pages=[1],
        generation={"finish_reason": "length"},
    )
    recovery = parse_native_observation(
        "<|det|>text [10,10,500,100]<|/det|>A short sentence with recovered detail."
        "<|det|>table [10,200,900,900]<|/det|><table><tr><td>DIFFERENT</td></tr></table>",
        mode="gundam",
        source_pages=[1],
    )
    blocks, provenance, warnings = reconcile_observations(primary, [recovery])
    assert blocks[0].markdown == "A short sentence with recovered detail."
    assert "DIFFERENT" not in blocks[1].markdown
    assert provenance[0]["recovery_observation"] == recovery.id
    assert "visual_truncated" in warnings


def test_clean_gundam_table_replaces_corrupt_page_local_columns():
    primary = parse_native_observation(
        "<|det|>title [100,100,200,120]<|/det|>Department"
        + "".join(
            f"<|det|>text [700,{120 + index * 10},800,{128 + index * 10}]<|/det|>[Non-Text]"
            for index in range(12)
        ),
        mode="multi_base",
        source_pages=[95],
    )
    recovery = parse_native_observation(
        "2014年1月1日\n<|det|>table [100,90,900,800]<|/det|>"
        "<table><tr><td>Department</td><td>Owner</td></tr><tr><td>Sales</td><td>Ada</td></tr></table>",
        mode="gundam",
        source_pages=[95],
    )
    blocks, provenance, warnings = reconcile_observations(primary, [recovery])
    assert [block.kind for block in blocks] == ["table"]
    assert provenance[0]["action"] == "replaced_corrupt_page_local_content"
    assert "visual_suspicious_ungrounded_preamble" in warnings


def test_split_multi_page_output_requires_one_segment_per_image():
    assert split_multi_page_output("<PAGE>\nOne\n<PAGE>\nTwo", 2) == ["One", "Two"]
    assert split_multi_page_output("<PAGE>\nMerged", 2) == ["Merged"]


def test_mlx_backend_uses_only_documented_model_contracts(monkeypatch, tmp_path: Path):
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        text = "One<PAGE>Two" if isinstance(kwargs["image"], list) else "Recovered"
        return SimpleNamespace(
            text=text,
            prompt_tokens=1,
            generation_tokens=2,
            finish_reason="stop",
            peak_memory=3.0,
        )

    monkeypatch.setitem(sys.modules, "mlx_vlm", SimpleNamespace(generate=generate))
    monkeypatch.setitem(
        sys.modules,
        "mlx_vlm.prompt_utils",
        SimpleNamespace(apply_chat_template=lambda processor, config, task, num_images: task),
    )
    backend = MlxUnlimitedOcr()
    backend._model = SimpleNamespace(config={})
    backend._processor = object()
    images = [tmp_path / "1.png", tmp_path / "2.png"]
    _, multi = backend.recognize_pages(images)
    _, gundam = backend.recognize(images[0])
    assert multi["contract"] == {
        "prompt": MULTI_PAGE_PROMPT,
        "base_size": 1024,
        "image_size": 1024,
        "crop_mode": False,
        "temperature": 0.0,
        "no_repeat_ngram_size": 35,
        "ngram_window": 1024,
    }
    assert gundam["contract"] == {
        "prompt": GUNDAM_PROMPT,
        "base_size": 1024,
        "image_size": 640,
        "crop_mode": True,
        "temperature": 0.0,
        "no_repeat_ngram_size": 35,
        "ngram_window": 128,
    }
    assert calls[0]["cropping"] is False and calls[0]["image_size"] == 1024
    assert calls[1]["cropping"] is True and calls[1]["image_size"] == 640


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


def test_repeated_headers_are_removed_and_cross_page_paragraphs_join():
    pages = []
    for number in range(1, 4):
        blocks = [
            Block("header", "Synthetic Book", bbox=(100, 10, 800, 40)),
            Block("paragraph", "A sentence that continues", bbox=(100, 100, 800, 900)),
        ]
        if number == 2:
            blocks[-1].markdown = "on the following page."
        pages.append(
            PageResult(
                number=number,
                image="",
                visual_markdown="",
                blocks=blocks,
                embedded=EmbeddedEvidence(),
                comparison=Comparison(),
            )
        )
    _normalize_document_blocks(pages)
    assert all(all(block.kind != "header" for block in page.blocks) for page in pages)
    assert pages[0].blocks[-1].markdown == "A sentence that continues on the following page."
    assert pages[0].blocks[-1].metadata["cross_page_paragraph"] is True


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
    resumed_metadata = json.loads((resumed / "metadata.json").read_text())
    assert resumed_metadata["resume_stable"] is True
    assert resumed_metadata["output_fingerprints"]


def test_recovery_observations_and_provenance_are_preserved(tmp_path: Path):
    pdf = tmp_path / "recovery.pdf"
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "A truncated local sentence with recovered detail.")
    document.save(pdf)
    document.close()
    bundle = convert(pdf, tmp_path / "out", backend=RecoveryFixtureOcr(), split_mode="single")
    page_json = json.loads((bundle / "pages/page-0001.json").read_text())
    assert page_json["visual"]["multi_page"]["raw_path"].startswith("raw/")
    assert len(page_json["visual"]["gundam"]) == 1
    assert page_json["recovery"][0]["action"] == "replaced_corrupt_page_local_content"
    for observation in [page_json["visual"]["multi_page"], *page_json["visual"]["gundam"]]:
        assert (bundle / observation["raw_path"]).exists()
    verification = verify_bundle(bundle)
    assert verification.ok, verification.errors


def test_interrupted_conversion_resumes_atomically_without_reprocessing(tmp_path: Path):
    pdf = tmp_path / "interrupted.pdf"
    document = fitz.open()
    for number in range(1, 4):
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {number} content.")
    document.save(pdf)
    document.close()
    backend = InterruptingFixtureOcr()
    try:
        convert(pdf, tmp_path / "out", backend=backend, split_mode="single", multi_page=False)
    except RuntimeError as error:
        assert "1 page(s) failed" in str(error)
    else:
        raise AssertionError("the first conversion should be interrupted")
    bundle = convert(pdf, tmp_path / "out", backend=backend, split_mode="single", multi_page=False)
    assert backend.calls == {1: 1, 2: 2, 3: 1}
    metadata = json.loads((bundle / "metadata.json").read_text())
    assert metadata["resume_stable"] is True
    assert verify_bundle(bundle).ok


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
    assert "| A   | B   |" in markdown
    assert "![Embedded figure]" not in markdown
    manifest = json.loads((bundle / "assets/manifest.json").read_text())
    assert manifest["assets"]
