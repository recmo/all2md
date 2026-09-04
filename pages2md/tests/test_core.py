from __future__ import annotations

import json
import sys
import zipfile
from types import SimpleNamespace
from pathlib import Path

import fitz
from PIL import Image, ImageDraw
import pytest

import pages2md.pipeline as pipeline
from pages2md.chapters import detect_chapters
from pages2md.cli import parser
from pages2md.compare import compare_text
from pages2md.ocr import GUNDAM_PROMPT, MULTI_PAGE_PROMPT, MlxUnlimitedOcr, _align_token_confidence, parse_output, split_multi_page_output
from pages2md.native import parse_native_observation, reconcile_observations
from pages2md.adapters import _link_target, detect_kind
from pages2md.formatting import FormatResult
from pages2md.pipeline import _align_multi_results, _apply_links_to_blocks, _canonicalize_figure_blocks, _convert_workspace, _intermediate_root, _is_visually_blank, _merge_continued_tables, _normalize_document_blocks, _ocr_groups, _repair_runaway_repetition, convert
from pages2md.model import Block, Comparison, EmbeddedEvidence, Link, PageResult, SourceDocument, SourcePage
from pages2md.verify import verify_bundle


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


class RepeatingFixtureOcr:
    identity = {"engine": "fixture", "model": "fixture", "revision": "1"}

    def recognize(self, image: Path):
        return (
            "<|det|>text [100,100,800,300]<|/det|>Useful introduction. "
            + "repeated phrase " * 20,
            {"mode": "multi_base", "finish_reason": "stop"},
        )


class LongFixtureOcr:
    identity = {"engine": "fixture", "model": "fixture", "revision": "1"}

    def recognize(self, image: Path):
        text = " ".join(f"token{index:05d}" for index in range(2_500))
        return (
            f"<|det|>text [100,100,800,300]<|/det|>{text}",
            {"mode": "multi_base", "finish_reason": "stop"},
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


def test_cli_has_one_input_and_force_only():
    arguments = parser().parse_args(["paper.pdf", "--force"])
    assert arguments.input == Path("paper.pdf")
    assert arguments.force is True


def test_file_workspace_name_strips_one_extension(tmp_path: Path):
    source = tmp_path / "TR26-164.PDF"
    source.touch()
    scans = tmp_path / "scans.v1"
    scans.mkdir()

    assert _intermediate_root(source) == tmp_path / "TR26-164.pages2md"
    assert _intermediate_root(scans) == tmp_path / "scans.v1.pages2md"


def test_legacy_nested_workspace_is_adopted_without_duplication(tmp_path: Path):
    source = tmp_path / "TR26-164.pdf"
    source.touch()
    legacy_root = tmp_path / "TR26-164.pdf.pages2md"
    legacy_bundle = legacy_root / "tr26-164-pdf"
    legacy_bundle.mkdir(parents=True)
    (legacy_bundle / "progress.json").write_text('{"status":"running"}')

    workspace = pipeline._prepare_intermediate_workspace(source)

    assert workspace == tmp_path / "TR26-164.pages2md"
    assert (workspace / "progress.json").exists()
    assert not legacy_root.exists()


def test_legacy_directory_workspace_is_flattened(tmp_path: Path):
    source = tmp_path / "scans.v1"
    source.mkdir()
    workspace = tmp_path / "scans.v1.pages2md"
    nested = workspace / "scans-v1"
    nested.mkdir(parents=True)
    (nested / "progress.json").write_text('{"status":"running"}')

    prepared = pipeline._prepare_intermediate_workspace(source)

    assert prepared == workspace
    assert (workspace / "progress.json").exists()
    assert not nested.exists()
    with pytest.raises(SystemExit):
        parser().parse_args(["convert", "paper.pdf"])
    with pytest.raises(SystemExit):
        parser().parse_args(["paper.pdf", "--output", "result"])


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


def test_clean_gundam_replaces_a_table_from_a_truncated_primary():
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
    assert "DIFFERENT" in blocks[1].markdown
    assert provenance[0]["recovery_observation"] == recovery.id
    assert "visual_truncated" in warnings


def test_three_pass_consensus_selects_majority_and_marks_disagreement():
    primary = parse_native_observation(
        r"<|det|>text [10,10,900,200]<|/det|>The value is \( a, a = e \).",
        mode="multi_base",
        source_pages=[27],
    )
    candidates = [
        parse_native_observation(
            r"<|det|>text [10,10,900,200]<|/det|>The value is \( a_i a = e \).",
            mode=mode,
            source_pages=[27],
        )
        for mode in ("gundam", "gundam_detail")
    ]
    blocks, provenance, warnings = reconcile_observations(primary, candidates)
    assert r"a_i a = e" in blocks[0].markdown
    assert blocks[0].metadata["review_required"] is True
    assert provenance[0]["action"] == "selected_ocr_consensus"
    assert "visual_ocr_disagreement" in warnings


def test_token_confidence_is_aligned_to_exact_decoded_pieces():
    class Tokenizer:
        pieces = {1: "a", 2: "_i", 3: " a"}

        def decode(self, token_ids, **_kwargs):
            return self.pieces[token_ids[0]]

    spans = _align_token_confidence(
        "a_i a",
        [(1, -0.01), (2, -0.5), (3, -0.02)],
        Tokenizer(),
        set(),
    )
    assert spans[1] == {"start": 1, "end": 3, "logprobs": [-0.5]}


def test_targeted_detail_uses_embedded_evidence_for_confident_base_error():
    primary = parse_native_observation(
        r"<|det|>text [10,10,900,200]<|/det|>cosets modulo \( N((a)) \)",
        mode="multi_base",
        source_pages=[25],
    )
    primary.blocks[0].metadata["uncertain_spans"] = [
        {"start": 0, "end": 6, "text": "cosets", "confidence": 0.6}
    ]
    detail = parse_native_observation(
        r"<|det|>text [10,10,900,200]<|/det|>cosets modulo \( N(\{a\}) \)",
        mode="gundam_detail",
        source_pages=[25],
        generation={"target_block_indices": [0]},
    )
    blocks, provenance, warnings = reconcile_observations(
        primary,
        [detail],
        embedded_text="cosets modulo N({a})",
    )
    assert r"N(\{a\})" in blocks[0].markdown
    assert provenance[0]["action"] == "selected_targeted_detail"
    assert not blocks[0].metadata.get("review_required")
    assert "visual_targeted_ocr_unresolved" not in warnings


def test_recovery_keeps_ungrounded_content_until_document_evidence_filtering():
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
    assert [block.kind for block in blocks] == ["paragraph", "table"]
    assert blocks[0].markdown == "2014年1月1日"
    assert provenance[0]["action"] == "replaced_corrupt_page_local_content"
    assert "visual_suspicious_ungrounded_preamble" not in warnings


def test_clean_recovery_replaces_repaired_runaway_without_text_overlap():
    primary = parse_native_observation(
        " ".join(["loop phrase"] * 20),
        mode="multi_base",
        source_pages=[17],
        generation={"finish_reason": "length"},
    )
    assert "visual_text_repetition" in primary.warnings
    recovery = parse_native_observation(
        r"Proof. For each \(0 \leq r < m\), define \(\mathcal K_r\).",
        mode="gundam_detail",
        source_pages=[17],
        generation={"target_block_indices": []},
    )

    blocks, provenance, warnings = reconcile_observations(primary, [recovery])

    assert blocks[0].markdown.startswith("Proof. For each")
    assert provenance[0]["action"] == "replaced_corrupt_page_local_content"
    assert "visual_text_repetition" in warnings


def test_nontruncated_repetition_still_requires_recovery_overlap():
    primary = parse_native_observation(
        " ".join(["loop phrase"] * 20),
        mode="multi_base",
        source_pages=[17],
        generation={"finish_reason": "stop"},
    )
    recovery = parse_native_observation(
        "Entirely unrelated but structurally valid recovery text.",
        mode="gundam_detail",
        source_pages=[17],
        generation={"target_block_indices": []},
    )

    blocks, provenance, _ = reconcile_observations(primary, [recovery])

    assert blocks[0].markdown.startswith("loop phrase")
    assert provenance == []


def test_split_multi_page_output_requires_one_segment_per_image():
    assert split_multi_page_output("<PAGE>\nOne\n<PAGE>\nTwo", 2) == ["One", "Two"]
    assert split_multi_page_output("<PAGE>\nMerged", 2) == ["Merged"]
    assert split_multi_page_output("<PAGE>\nOne A\n<PAGE>\nOne B\n<PAGE>\nTwo", 2) == [
        "One A", "One B", "Two"
    ]


def test_oversegmented_multi_page_output_is_merged_monotonically(tmp_path: Path):
    image = tmp_path / "page.png"
    Image.new("RGB", (10, 10), "white").save(image)
    group = [
        SourcePage(1, image, embedded=EmbeddedEvidence(text="Alpha first continuation")),
        SourcePage(2, image, embedded=EmbeddedEvidence(text="Beta second")),
    ]
    recognized = [
        ("Alpha first", {"mode": "multi_base"}),
        ("continuation", {"mode": "multi_base"}),
        ("Beta second", {"mode": "multi_base"}),
    ]

    aligned = _align_multi_results(group, recognized)

    assert [item[0] for item in aligned] == ["Alpha first\ncontinuation", "Beta second"]
    assert aligned[0][1]["merged_output_segments"] == 2
    assert aligned[1][1]["merged_output_segments"] == 1


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
    _, detail = backend.recognize_detail(images[0])
    assert multi["contract"] == {
        "prompt": MULTI_PAGE_PROMPT,
        "base_size": 1024,
        "image_size": 1024,
        "crop_mode": False,
        "temperature": 0.0,
        "no_repeat_ngram_size": 35,
        "ngram_window": 1024,
        "precision": {"vision": "float32", "decoder": "bfloat16"},
    }
    assert gundam["contract"] == {
        "prompt": GUNDAM_PROMPT,
        "base_size": 1024,
        "image_size": 640,
        "crop_mode": True,
        "temperature": 0.0,
        "no_repeat_ngram_size": 35,
        "ngram_window": 128,
        "precision": {"vision": "float32", "decoder": "bfloat16"},
    }
    assert detail["contract"] == {
        "prompt": GUNDAM_PROMPT,
        "base_size": 1024,
        "image_size": 1024,
        "crop_mode": True,
        "temperature": 0.0,
        "no_repeat_ngram_size": 35,
        "ngram_window": 128,
        "precision": {"vision": "float32", "decoder": "bfloat16"},
    }
    assert calls[0]["cropping"] is False and calls[0]["image_size"] == 1024
    assert calls[1]["cropping"] is True and calls[1]["image_size"] == 640
    assert calls[2]["cropping"] is True and calls[2]["image_size"] == 1024


def test_mlx_backend_suppresses_model_load_stdout(monkeypatch, capsys):
    def load(*args, **kwargs):
        print("Add pad token = ['<pad>']")
        print("Added chat tokens")
        return object(), object()

    monkeypatch.setitem(sys.modules, "mlx_vlm", SimpleNamespace(load=load))
    backend = MlxUnlimitedOcr()
    monkeypatch.setattr(backend, "_configure_precision", lambda: None)

    backend._load()

    assert capsys.readouterr().out == ""


def test_mlx_backend_preserves_model_load_errors(monkeypatch, capsys):
    def load(*args, **kwargs):
        print("Added grounding-related tokens")
        raise RuntimeError("model load failed")

    monkeypatch.setitem(sys.modules, "mlx_vlm", SimpleNamespace(load=load))
    backend = MlxUnlimitedOcr()

    with pytest.raises(RuntimeError, match="model load failed"):
        backend._load()

    assert capsys.readouterr().out == ""


def test_mlx_backend_enforces_component_precision():
    import mlx.core as mx

    class Component:
        dtype = None

        def set_dtype(self, dtype):
            self.dtype = dtype

    processor = SimpleNamespace(
        process_one=lambda: {"images": [mx.ones((1,), dtype=mx.bfloat16)]}
    )
    backend = MlxUnlimitedOcr()
    backend._model = SimpleNamespace(
        sam_model=Component(),
        vision_model=Component(),
        projector=Component(),
        language_model=Component(),
    )
    backend._processor = processor
    backend._configure_precision()
    assert backend._model.sam_model.dtype == mx.float32
    assert backend._model.vision_model.dtype == mx.float32
    assert backend._model.projector.dtype == mx.float32
    assert backend._model.language_model.dtype == mx.bfloat16
    assert processor.process_one()["images"][0].dtype == mx.float32


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


def test_single_markdown_with_figures_publishes_only_final_artifacts(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (100, 100), "navy").save(image_path)
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "broken embedded text")
    page.insert_image(fitz.Rect(100, 300, 250, 450), filename=str(image_path))
    document.save(pdf)
    document.close()

    bundle = convert(pdf, backend=FixtureOcr())
    assert bundle == tmp_path / "paper.md"
    published = sorted(str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file())
    assert published[-1] == "paper.md"
    assert len(published) == 2
    assert published[0].startswith("figures/fig-")
    markdown = (bundle / "paper.md").read_text()
    assert markdown.startswith("---\nsource_sha256: ")
    assert "\npages2md_version: " in markdown.split("---", 2)[1]
    assert "The visual text has $x^2$." in markdown
    assert "broken embedded text" not in markdown
    assert "![A useful diagram.]" in markdown
    assert "![Embedded figure]" not in markdown
    verification = verify_bundle(bundle)
    assert verification.ok, verification.errors


def test_ocr_detected_figure_prefers_matching_embedded_pdf_image(tmp_path: Path):
    class MatchingFigureOcr:
        identity = {"engine": "fixture", "model": "fixture", "revision": "1"}

        def recognize(self, image: Path):
            return (
                "<|det|>diagram [163, 379, 409, 568]<|/det|>A matched diagram.",
                {"finish_reason": "stop"},
            )

    pdf = tmp_path / "matched.pdf"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (100, 100), "navy").save(image_path)
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(100, 300, 250, 450), filename=str(image_path))
    document.save(pdf)
    document.close()

    bundle = convert(pdf, backend=MatchingFigureOcr())
    figures = list((bundle / "figures").iterdir())
    assert len(figures) == 1
    with Image.open(figures[0]) as figure:
        assert figure.size == (100, 100)
    assert "![A matched diagram.]" in (bundle / "matched.md").read_text()


def test_pdf_link_targets_accept_string_page_numbers():
    assert _link_target({"page": "15"}) == "#page-16"
    assert _link_target({"page": 15}) == "#page-16"
    assert _link_target({"uri": "https://example.com", "page": "15"}) == "https://example.com"
    assert _link_target({"page": "named-destination"}) == ""


def test_blank_pages_are_not_grouped_with_content(tmp_path: Path):
    blank = tmp_path / "blank.png"
    content = tmp_path / "content.png"
    Image.new("RGB", (300, 300), "white").save(blank)
    content_image = Image.new("RGB", (300, 300), "white")
    for x in range(50, 250):
        for y in range(100, 130):
            content_image.putpixel((x, y), (0, 0, 0))
    content_image.save(content)

    assert _is_visually_blank(blank)[0] is True
    assert _is_visually_blank(content)[0] is False
    pages = [
        SourcePage(1, content),
        SourcePage(3, content),
    ]
    assert [[page.number for page in group] for group in _ocr_groups(pages, [])] == [[1], [3]]


def test_multi_page_ocr_windows_are_bounded_for_atomic_progress(tmp_path: Path):
    image = tmp_path / "page.png"
    Image.new("RGB", (10, 10), "white").save(image)
    pages = [SourcePage(number, image) for number in range(1, 21)]

    groups = _ocr_groups(pages, [])

    assert [[page.number for page in group] for group in groups] == [
        list(range(1, 9)),
        list(range(9, 17)),
        list(range(17, 21)),
    ]


def test_conversion_progress_counts_completed_pages(tmp_path: Path, monkeypatch):
    progress_bars = []

    class RecordingProgress:
        def __init__(self, **kwargs):
            self.total = kwargs["total"]
            self.completed = 0
            self.postfixes = []
            progress_bars.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def set_postfix_str(self, value, **kwargs):
            self.postfixes.append(value)

        def update(self, amount=1):
            self.completed += amount

    monkeypatch.setattr("pages2md.pipeline.tqdm", RecordingProgress)
    pdf = tmp_path / "progress.pdf"
    document = fitz.open()
    for number in range(1, 4):
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {number} content.")
    document.save(pdf)
    document.close()

    convert(pdf, backend=FixtureOcr())

    assert len(progress_bars) == 1
    assert progress_bars[0].total == 3
    assert progress_bars[0].completed == 3
    assert "processing 1-3" in progress_bars[0].postfixes


def test_document_normalization_drops_running_matter_without_inventing_semantics():
    page = PageResult(
        number=25,
        image="page.png",
        visual_markdown="",
        blocks=[
            Block("header", "2. Rings and Fields", bbox=(100, 60, 300, 80)),
            Block("page_number", "11", bbox=(850, 60, 880, 80)),
            Block(
                "paragraph",
                "1.28. Definition. A ring is a set with two binary operations.",
                bbox=(100, 400, 900, 500),
            ),
            Block(
                "heading",
                "1.31. Theorem. Every finite integral domain is a field.",
                bbox=(100, 800, 900, 840),
            ),
        ],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )

    _normalize_document_blocks([page])

    assert [(block.kind, block.markdown) for block in page.blocks] == [
        ("paragraph", "1.28. Definition. A ring is a set with two binary operations."),
        ("heading", "1.31. Theorem. Every finite integral domain is a field."),
    ]


def test_document_normalization_drops_repeated_ungrounded_running_headers():
    pages = [
        PageResult(
            number=number,
            image="page.png",
            visual_markdown="",
            blocks=[
                Block("paragraph", "Exercises", metadata={"native_ungrounded": True}),
                Block("paragraph", str(70 + number), metadata={"native_ungrounded": True}),
                Block("paragraph", f"Exercise content for page {number}."),
            ],
            embedded=EmbeddedEvidence(),
            comparison=Comparison(),
        )
        for number in (1, 2)
    ]

    _normalize_document_blocks(pages)

    assert [[block.markdown for block in page.blocks] for page in pages] == [
        ["Exercise content for page 1."],
        ["Exercise content for page 2."],
    ]


def test_document_normalization_drops_unsupported_ungrounded_preamble():
    page = PageResult(
        number=26,
        image="page.png",
        visual_markdown="",
        blocks=[
            Block("paragraph", "2014年1月1日", metadata={"native_ungrounded": True}),
            Block("heading", "Chapter 3", bbox=(100, 100, 900, 150)),
        ],
        embedded=EmbeddedEvidence(text="Chapter 3"),
        comparison=Comparison(),
    )

    _normalize_document_blocks([page])

    assert [block.markdown for block in page.blocks] == ["Chapter 3"]
    assert "visual_unsupported_ungrounded_text" in page.warnings


def test_document_normalization_keeps_embedded_supported_ungrounded_content():
    continuation = "3. The hiring manager conducts three reference interviews."
    page = PageResult(
        number=27,
        image="page.png",
        visual_markdown="",
        blocks=[
            Block("paragraph", continuation, metadata={"native_ungrounded": True}),
            Block("paragraph", "Here's a sample script:", bbox=(100, 200, 900, 230)),
        ],
        embedded=EmbeddedEvidence(text=f"{continuation}\n\nHere's a sample script:"),
        comparison=Comparison(),
    )

    _normalize_document_blocks([page])

    assert page.blocks[0].markdown == continuation
    assert page.blocks[0].metadata["embedded_token_support"] == 1.0


def test_document_normalization_keeps_ungrounded_only_result_as_best_available_ocr():
    page = PageResult(
        number=28,
        image="page.png",
        visual_markdown="",
        blocks=[Block("paragraph", "Only OCR result", metadata={"native_ungrounded": True})],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )

    _normalize_document_blocks([page])

    assert [block.markdown for block in page.blocks] == ["Only OCR result"]


def test_document_normalization_cleans_prose_without_rewriting_math():
    page = PageResult(
        number=27,
        image="page.png",
        visual_markdown="",
        blocks=[
            Block("paragraph", r"Proof.  Let \( J \) be an ideal and \( J = \langle ra : r \in R \rangle \) ."),
            Block("paragraph", "(i) First case."),
            Block("paragraph", "(ii) Second case."),
            Block(
                "paragraph",
                r"If \( a \equiv b \mod J \), then \( na \equiv nh \mod J \) for \(n \in \mathbb{Z}\).",
            ),
        ],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )
    _normalize_document_blocks([page])
    assert page.blocks[0].markdown == r"Proof. Let \( J \) be an ideal and \( J = \langle ra : r \in R \rangle \)."
    assert page.blocks[1].kind == "list"
    assert page.blocks[1].markdown == "- **(i)** First case.\n- **(ii)** Second case."
    assert r"na \equiv nh \mod J" in page.blocks[2].markdown
    assert r"\mathbb{Z}" in page.blocks[2].markdown
    assert r"\\)" not in page.blocks[0].markdown


def test_document_normalization_does_not_infer_caption_from_wording():
    page = PageResult(
        number=28,
        image="page.png",
        visual_markdown="",
        blocks=[
            Block("figure", "Diagram", bbox=(100, 100, 900, 600)),
            Block("paragraph", "Figure 3 discusses the result.", bbox=(100, 620, 900, 680)),
        ],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )

    _normalize_document_blocks([page])

    assert [block.kind for block in page.blocks] == ["figure", "paragraph"]
    assert page.blocks[1].markdown == "Figure 3 discusses the result."


def test_recovery_observations_and_provenance_are_preserved(tmp_path: Path):
    pdf = tmp_path / "recovery.pdf"
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "A truncated local sentence with recovered detail.")
    document.save(pdf)
    document.close()
    bundle = _convert_workspace(pdf, tmp_path / "out", backend=RecoveryFixtureOcr())
    page_json = json.loads((bundle / "pages/page-0001.json").read_text())
    assert page_json["visual"]["multi_page"]["raw_path"].startswith("raw/")
    assert len(page_json["visual"]["candidates"]) == 1
    assert page_json["recovery"][0]["action"] == "replaced_corrupt_page_local_content"
    for observation in [page_json["visual"]["multi_page"], *page_json["visual"]["candidates"]]:
        assert (bundle / observation["raw_path"]).exists()
    verification = verify_bundle(bundle)
    assert verification.ok, verification.errors


def test_failed_conversion_retains_intermediate_results_for_resume(tmp_path: Path):
    pdf = tmp_path / "interrupted.pdf"
    document = fitz.open()
    for number in range(1, 4):
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {number} content.")
    document.save(pdf)
    document.close()
    class FailingOnce(FixtureOcr):
        def __init__(self):
            self.failed = False

        def recognize_pages(self, images):
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated interruption")
            return FixtureOcr.recognize(self, images[0])

    backend = FailingOnce()
    try:
        convert(pdf, backend=backend)
    except RuntimeError as error:
        assert "3 page(s) failed" in str(error)
    else:
        raise AssertionError("the first conversion should be interrupted")
    assert not (tmp_path / "interrupted.md").exists()
    intermediate = _intermediate_root(pdf)
    assert (intermediate / "progress.json").exists()
    assert json.loads((intermediate / "progress.json").read_text())["status"] == "failed"
    bundle = convert(pdf, backend=backend)
    assert bundle == tmp_path / "interrupted.md"
    assert verify_bundle(bundle).ok
    assert json.loads((intermediate / "progress.json").read_text())["status"] == "complete"


def test_resume_processes_only_pages_missing_from_persistent_workspace(tmp_path: Path):
    class CountingFixture(FixtureOcr):
        def __init__(self):
            self.calls = 0

        def recognize(self, image: Path):
            self.calls += 1
            number = int(image.stem.rsplit("-", 1)[-1])
            return (
                f"<|det|>text [100,100,800,300]<|/det|>Page {number} content.",
                {"finish_reason": "stop"},
            )

    pdf = tmp_path / "resume.pdf"
    document = fitz.open()
    for number in range(1, 4):
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {number} content.")
    document.save(pdf)
    document.close()
    backend = CountingFixture()

    bundle = _convert_workspace(pdf, _intermediate_root(pdf), backend=backend)
    first_run_calls = backend.calls
    assert first_run_calls == 3
    (bundle / "pages/page-0003.json").unlink()

    resumed = _convert_workspace(pdf, _intermediate_root(pdf), backend=backend)

    assert resumed == bundle
    assert backend.calls == first_run_calls + 1
    assert (bundle / "pages/page-0003.json").exists()
    assert json.loads((bundle / "progress.json").read_text())["completed_pages"] == [1, 2, 3]


def test_code_change_reprocesses_checkpoints_without_repeating_ocr(tmp_path: Path, monkeypatch):
    class CountingFixture(FixtureOcr):
        def __init__(self):
            self.calls = 0

        def recognize(self, image: Path):
            self.calls += 1
            return super().recognize(image)

    pdf = tmp_path / "code-change.pdf"
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "The visual text has x squared.")
    document.save(pdf)
    document.close()
    backend = CountingFixture()
    code_revision = {"value": "v1"}
    original_fingerprint = pipeline._code_fingerprint

    def versioned_fingerprint(*names):
        return f"{original_fingerprint(*names)}-{code_revision['value']}"

    monkeypatch.setattr(pipeline, "_code_fingerprint", versioned_fingerprint)
    bundle = _convert_workspace(pdf, _intermediate_root(pdf), backend=backend)
    assert backend.calls == 1
    checkpoint = bundle / "pages/page-0001.json"
    assert checkpoint.exists()

    reconciliations = 0
    original_reconcile = pipeline.reconcile_observations

    def record_reconciliation(*args, **kwargs):
        nonlocal reconciliations
        reconciliations += 1
        return original_reconcile(*args, **kwargs)

    monkeypatch.setattr(pipeline, "reconcile_observations", record_reconciliation)
    code_revision["value"] = "v2"
    resumed = _convert_workspace(pdf, _intermediate_root(pdf), backend=backend)

    assert resumed == bundle
    assert backend.calls == 1
    assert reconciliations == 1
    assert checkpoint.exists()


def test_incompatible_checkpoint_is_retained_until_force(tmp_path: Path):
    pdf = tmp_path / "backend-change.pdf"
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "Source text.")
    document.save(pdf)
    document.close()
    bundle = _convert_workspace(pdf, _intermediate_root(pdf), backend=FixtureOcr())
    checkpoint = bundle / "pages/page-0001.json"

    incompatible = FixtureOcr()
    incompatible.identity = {**FixtureOcr.identity, "revision": "2"}
    with pytest.raises(RuntimeError, match="incompatible intermediate bundle retained"):
        _convert_workspace(pdf, _intermediate_root(pdf), backend=incompatible)

    assert checkpoint.exists()


def test_page_checkpoint_survives_interruption_and_resume(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "page-interruption.pdf"
    document = fitz.open()
    for number in range(1, 4):
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {number} content.")
    document.save(pdf)
    document.close()

    original_page_result = pipeline._page_result
    original_normalize = pipeline._normalize_document_blocks
    interrupted = False
    normalized_page_sets = []

    def fail_once(source_page, *args, **kwargs):
        nonlocal interrupted
        if source_page.number == 2 and not interrupted:
            interrupted = True
            raise RuntimeError("simulated page interruption")
        return original_page_result(source_page, *args, **kwargs)

    def record_normalization(pages):
        normalized_page_sets.append([page.number for page in pages])
        return original_normalize(pages)

    monkeypatch.setattr(pipeline, "_page_result", fail_once)
    monkeypatch.setattr(pipeline, "_normalize_document_blocks", record_normalization)
    backend = FixtureOcr()
    try:
        convert(pdf, backend=backend)
    except RuntimeError as error:
        assert "1 page(s) failed" in str(error)
    else:
        raise AssertionError("the first conversion should be interrupted")

    intermediate = _intermediate_root(pdf)
    assert (intermediate / "pages/page-0001.json").exists()
    assert not (intermediate / "pages/page-0002.json").exists()
    assert (intermediate / "pages/page-0003.json").exists()
    progress = json.loads((intermediate / "progress.json").read_text())
    assert progress["completed_pages"] == [1, 3]
    assert progress["status"] == "failed"
    assert normalized_page_sets == []

    output = convert(pdf, backend=backend)
    assert output == tmp_path / "page-interruption.md"
    assert json.loads((intermediate / "progress.json").read_text())["completed_pages"] == [1, 2, 3]
    assert normalized_page_sets == [[1, 2, 3]]


def test_content_quality_warning_does_not_suppress_output(tmp_path: Path, capsys):
    pdf = tmp_path / "repetition.pdf"
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "repeated phrase")
    document.save(pdf)
    document.close()

    workspace = _convert_workspace(pdf, tmp_path / "workspace", backend=RepeatingFixtureOcr())
    verification = verify_bundle(workspace)
    assert verification.ok, verification.errors
    assert "visual_text_repetition_repaired" in verification.warnings
    assert not any("needs content review: visual_text_repetition" in warning for warning in verification.warnings)

    output = convert(pdf, backend=RepeatingFixtureOcr())
    assert output == tmp_path / "repetition.md"
    assert output.is_file()
    assert "Useful introduction." in output.read_text()
    assert "repeated phrase" in output.read_text()
    assert output.read_text().count("repeated phrase") == 1

    long_pdf = tmp_path / "long.pdf"
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "source text")
    document.save(long_pdf)
    document.close()
    long_output = convert(long_pdf, backend=LongFixtureOcr())
    captured = capsys.readouterr()
    assert long_output.is_file()
    assert "needs content review: visual_implausible_output_length" in captured.err


def test_markdown_lint_findings_are_warnings_and_do_not_suppress_output(tmp_path: Path, monkeypatch, capsys):
    pdf = tmp_path / "linted.pdf"
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "A document with a lint finding.")
    document.save(pdf)
    document.close()

    monkeypatch.setattr(
        "pages2md.pipeline.format_and_lint",
        lambda paths: FormatResult(lint_errors=["book.md:1: MD999 test finding"]),
    )

    output = convert(pdf, backend=FixtureOcr())

    assert output.is_file()
    captured = capsys.readouterr()
    assert "metadata reports Markdown lint failures" in captured.err
    assert "markdown lint: book.md:1: MD999 test finding" in captured.err


def test_figure_crops_reject_only_blank_and_near_duplicate_boxes(tmp_path: Path):
    image_path = tmp_path / "page.png"
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((300, 300, 700, 700), outline="black", width=8)
    draw.line((300, 500, 700, 500), fill="black", width=8)
    image.save(image_path)
    blocks = [
        Block("figure", "", bbox=(290, 290, 710, 710)),
        Block("figure", "Important caption", bbox=(292, 292, 708, 708)),
        Block("figure", "", bbox=(350, 350, 650, 650)),
        Block("figure", "", bbox=(750, 750, 850, 850)),
    ]
    warnings = _canonicalize_figure_blocks(blocks, image_path)
    assert len(blocks) == 2
    assert blocks[0].bbox == (284.0, 284.0, 716.0, 716.0)
    assert blocks[0].markdown == "Important caption"
    assert blocks[1].bbox == (342.0, 342.0, 658.0, 658.0)
    assert blocks[1].metadata["review_reason"] == "figure_crop_touches_edge"
    assert warnings == [
        "visual_blank_figure_crop_rejected",
        "visual_duplicate_figure_crop_rejected",
        "visual_figure_crop_may_be_clipped",
    ]


def test_figure_crops_retain_full_bleed_text_heavy_and_margin_images(tmp_path: Path):
    image_path = tmp_path / "page.png"
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((42, 42, 258, 258), fill="navy")
    draw.rectangle((300, 300, 700, 600), outline="black", width=8)
    draw.text((320, 330), "A text-heavy screenshot " * 5, fill="black")
    draw.line((210, 920, 790, 920), fill="black", width=8)
    draw.line((50, 750, 250, 750), fill=(252, 252, 252), width=8)
    image.save(image_path)
    blocks = [
        Block("figure", "", bbox=(50, 50, 250, 250)),
        Block("figure", "", bbox=(300, 300, 700, 600)),
        Block("figure", "", bbox=(200, 870, 800, 970)),
        Block("figure", "Faint diagram", bbox=(40, 700, 260, 800)),
    ]

    warnings = _canonicalize_figure_blocks(blocks, image_path)

    assert len(blocks) == 4
    assert blocks[3].markdown == "Faint diagram"
    assert blocks[0].metadata["review_reason"] == "figure_crop_touches_edge"
    assert warnings == ["visual_figure_crop_may_be_clipped"]


def test_repetition_across_blocks_removes_only_the_proven_loop():
    blocks = [
        Block("paragraph", f"Answer\nUnique section {index}")
        for index in range(5)
    ]
    blocks.extend(
        Block("paragraph", "loop phrase")
        for _ in range(20)
    )
    blocks.append(Block("paragraph", "Legitimate conclusion."))

    warnings = _repair_runaway_repetition(blocks)

    assert [block.markdown for block in blocks] == [
        *[f"Answer\nUnique section {index}" for index in range(5)],
        "loop phrase",
        "Legitimate conclusion.",
    ]
    assert warnings == ["visual_text_repetition_repaired"]


def test_blank_figure_does_not_create_a_full_page_fallback(tmp_path: Path):
    image_path = tmp_path / "page.png"
    Image.new("RGB", (1000, 1000), "white").save(image_path)
    blocks = [
        Block("figure", "", bbox=(100, 100, 200, 200)),
        Block("figure", "Important caption", bbox=(300, 300, 400, 400)),
    ]

    warnings = _canonicalize_figure_blocks(blocks, image_path)

    assert len(blocks) == 1
    assert blocks[0].kind == "paragraph"
    assert blocks[0].markdown == "Important caption"
    assert blocks[0].bbox is None
    assert blocks[0].metadata["review_reason"] == "blank_figure_crop_caption_preserved"
    assert warnings == ["visual_blank_figure_crop_rejected"]


def test_embedded_links_are_geometry_aware_and_idempotent():
    blocks = [
        Block("paragraph", "Foo first", bbox=(0, 0, 1000, 200)),
        Block("paragraph", "Foo second", bbox=(0, 400, 1000, 600)),
    ]
    links = [Link("Foo", "https://example.com", bbox=(100, 450, 200, 500))]
    _apply_links_to_blocks(blocks, links)
    _apply_links_to_blocks(blocks, links)
    assert blocks[0].markdown == "Foo first"
    assert blocks[1].markdown == "[Foo](https://example.com) second"


def test_embedded_table_image_does_not_create_a_figure_without_ocr_claim(tmp_path: Path):
    pdf = tmp_path / "table.pdf"
    image_path = tmp_path / "table.png"
    table_image = Image.new("RGB", (200, 200), "white")
    for coordinate in (20, 100, 180):
        for offset in range(3):
            for value in range(20, 181):
                table_image.putpixel((coordinate + offset, value), (0, 0, 0))
                table_image.putpixel((value, coordinate + offset), (0, 0, 0))
    table_image.save(image_path)
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(92, 277, 257, 475), filename=str(image_path))
    document.save(pdf)
    document.close()
    bundle = convert(pdf, backend=TableFixtureOcr())
    markdown = bundle.read_text()
    assert "| A   | B   |" in markdown
    assert "![Embedded figure]" not in markdown
    assert bundle == tmp_path / "table.md"


def test_reused_pdf_image_records_distinct_placements(tmp_path: Path):
    pdf = tmp_path / "reused.pdf"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (100, 100), "navy").save(image_path)
    document = fitz.open()
    first = document.new_page(width=612, height=792)
    xref = first.insert_image(fitz.Rect(100, 300, 250, 450), filename=str(image_path))
    second = document.new_page(width=612, height=792)
    second.insert_image(fitz.Rect(300, 200, 450, 350), xref=xref)
    document.save(pdf)
    document.close()

    bundle = _convert_workspace(
        pdf,
        tmp_path / "out",
        backend=FixtureOcr(),
    )
    manifest = json.loads((bundle / "assets/manifest.json").read_text())
    embedded = next(
        asset for asset in manifest["assets"]
        if asset.get("source_object") == f"xref:{xref}"
    )
    assert {placement["page"] for placement in embedded["placements"]} == {1, 2}
    assert all(placement["bbox"] for placement in embedded["placements"])


def test_epub_is_rejected_instead_of_treated_as_cbz(tmp_path: Path):
    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            'version="1.0"><rootfiles><rootfile full-path="OEBPS/package.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
    with pytest.raises(ValueError, match="unsupported input format"):
        detect_kind(epub)


def test_cbz_remains_supported_after_epub_removal(tmp_path: Path):
    cbz = tmp_path / "pages.cbz"
    with zipfile.ZipFile(cbz, "w") as archive:
        archive.writestr("001.png", b"image fixture")
    assert detect_kind(cbz) == "cbz"
