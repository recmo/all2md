from types import SimpleNamespace

from PIL import Image

from pages2md.latex import clean_latex
from pages2md.model import Block, OcrObservation, EmbeddedEvidence
from pages2md.quality import mathematical_runaway, runaway_repetition_span
from pages2md.region_recovery import recover_regions, select_candidates, _crop_observation
from pages2md.regions import source_inventory, audit_regions, preserves_coverage
from test_alignment import evidence, BOX


def test_source_inventory_does_not_accept_box_overlap_as_transcription():
    native = evidence([("Missing equation xyz=123", 20, 100, 10, "Times-Roman")])
    inventory = source_inventory(1, native)
    audit = audit_regions(inventory, [Block("paragraph", "Unrelated prose", BOX)], native)
    assert audit["status"] == "needs_review"
    assert any(f["kind"] == "source_content_unresolved" for f in audit["findings"])


def test_cached_candidate_recovers_content_without_discarding_matched_glyphs():
    native = evidence([("The missing equation xyz=123", 20, 100, 10, "Times-Roman")])
    inventory = source_inventory(1, native)
    initial = [Block("paragraph", "The missing equation", BOX)]
    candidate = OcrObservation("test", "detail", "", [1],
                               blocks=[Block("paragraph", "The missing equation xyz=123", BOX)])
    blocks, audit, attempts = select_candidates(initial, [candidate], inventory, native)
    assert blocks[0].markdown.endswith("xyz=123")
    assert attempts[0]["accepted"]
    assert preserves_coverage(audit_regions(inventory, initial, native), audit)
    assert initial[0].markdown == "The missing equation"


def test_candidate_cannot_trade_away_known_content():
    native = evidence([("abcdefghij klmnopqrst", 20, 100, 10, "Times-Roman")])
    inventory = source_inventory(1, native)
    initial = [Block("paragraph", "abcdefghij", BOX)]
    candidate = OcrObservation("test", "detail", "", [1],
                               blocks=[Block("paragraph", "klmnopqrst", BOX)])
    blocks, _, attempts = select_candidates(initial, [candidate], inventory, native)
    assert blocks == initial
    assert not attempts[0]["accepted"]


def test_math_runaway_with_changing_indices_is_detected_not_truncated():
    text = ", ".join(rf"\(\alpha_{{{i}}}\)" for i in range(150))
    assert mathematical_runaway(text)
    assert runaway_repetition_span(text) is None
    assert not mathematical_runaway(r"\(\alpha_1,\ldots,\alpha_n\)")


def test_nested_text_and_environment_formatting_are_idempotent():
    text = r"\[\begin{array}{c} P (X), | c | \\[2pt] \text{keep {P (X)} and | c |} \\ \begin{matrix} a \\ b \end{matrix} \end{array}\]"
    cleaned = clean_latex(text)
    assert "P(X), |c|" in cleaned
    assert r"\text{keep {P (X)} and | c |}" in cleaned
    assert "\\\\[2pt]\n" in cleaned
    assert "\\begin{matrix}\na \\\\\nb\n\\end{matrix}" in cleaned
    assert clean_latex(cleaned) == cleaned


def test_crop_failure_is_cached_and_replay_makes_no_calls(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(path)
    native = evidence([("The missing equation xyz=123", 20, 100, 10, "Times-Roman")])
    source = SimpleNamespace(number=1, embedded=native, image_path=path)
    class Backend:
        identity = {"name": "test"}
        calls = 0
        def recognize_detail(self, path):
            self.calls += 1
            raise RuntimeError("offline")
    backend = Backend()
    initial = [Block("paragraph", "The missing equation", BOX)]
    recover_regions(source, initial, [], backend=backend, bundle=tmp_path, budget=2)
    assert backend.calls == 2
    recover_regions(source, initial, [], backend=backend, bundle=tmp_path, budget=2)
    assert backend.calls == 2
    _, replay = recover_regions(source, initial, [], bundle=tmp_path)
    assert backend.calls == 2
    assert len([a for a in replay["attempts"] if a.get("error") == "RuntimeError"]) == 2
    # A broken ancillary response must not cause expensive page OCR to restart.
    (tmp_path / "region-observations" / "broken.json").write_text("{")
    _, replay = recover_regions(source, initial, [], bundle=tmp_path)
    assert any(a.get("error") == "invalid_cache" for a in replay["attempts"])


def test_raster_inventory_distinguishes_association_from_transcription(tmp_path):
    from PIL import ImageDraw
    path = tmp_path / "scan.png"
    image = Image.new("RGB", (300, 300), "white")
    ImageDraw.Draw(image).text((30, 60), "Missing printed words", fill="black")
    image.save(path)
    native = EmbeddedEvidence()
    inventory = source_inventory(1, native, path)
    assert inventory
    audit = audit_regions(inventory, [Block("paragraph", "words", BOX)], native)
    assert all(r["status"] == "visually_associated" for r in audit["regions"])


def test_proof_square_requires_shape_and_context():
    from PIL import ImageDraw
    from pages2md.pipeline import _visual_proof_square
    image = Image.new("L", (1000, 1000), 255)
    ImageDraw.Draw(image).rectangle((867, 225, 878, 236), outline=0, width=2)
    block = Block("figure", "", (855, 215, 890, 245))
    preceding = [Block("paragraph", "as desired.", (100, 220, 200, 240))]
    assert _visual_proof_square(image, block, preceding)
    preceding[0].markdown = "This completes the proof of Theorem 1.16."
    assert _visual_proof_square(image, block, preceding)
    assert not _visual_proof_square(image, block, [])
    preceding[0].markdown = "An example diagram."
    assert not _visual_proof_square(image, block, preceding)


def test_heading_math_and_multiple_late_boundaries_survive():
    from pages2md.markdown import strict_page_markdown
    from pages2md.model import PageResult, Comparison
    page = PageResult(5, "", "", [
        Block("paragraph", "Continued proof.", (100, 100, 900, 300)),
        Block("heading", "5\nBounding " + r"\(\Lambda\)", (100, 600, 800, 620)),
        Block("heading", "5.1\nThe next step", (100, 800, 800, 820)),
    ], EmbeddedEvidence(), Comparison())
    text = strict_page_markdown(page, [
        {"page": 5, "title": "5 Bounding", "level": 1},
        {"page": 5, "title": "5.1 The next step", "level": 2},
    ])
    assert "\n# 5 Bounding " + r"\(\Lambda\)" in text
    assert "## 5.1 The Next Step" in text
    assert text.index("Continued") < text.index("# 5")
    assert text.count("Bounding") == 1


def test_crop_coordinates_map_back_to_page():
    value = {"raw": "<|det|>text [100,200,900,800]<|/det|>Recovered text",
             "page": 7, "bbox": (100, 300, 500, 600), "generation": {}}
    observation = _crop_observation(value)
    assert observation.blocks[0].bbox == (140, 360, 460, 540)
    assert observation.source_pages == [7]


def test_unmatched_sizing_delimiter_triggers_recovery():
    native = evidence([("abc", 20, 100, 10, "Times-Roman")])
    audit = audit_regions(source_inventory(1, native),
                          [Block("formula", r"\[\left\{abc\]", BOX)], native)
    assert any(f["kind"] == "unbalanced_sizing_delimiters" for f in audit["findings"])
    from pages2md.texstructure import balanced_sizing_delimiters
    assert balanced_sizing_delimiters(r"\left\{a + \left(b\right)\right.")
    assert balanced_sizing_delimiters(r"\text{\left} + x")


def test_cli_explicit_fresh_recovery(monkeypatch):
    from pages2md import cli
    calls = []
    monkeypatch.setattr(cli, "convert", lambda source, **options: calls.append(options))
    cli.main(["--recover-regions", "paper.pdf"])
    assert calls[0]["recover_regions_fresh"] is True
