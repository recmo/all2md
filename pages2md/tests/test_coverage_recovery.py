from pages2md.model import Block, EmbeddedEvidence
from pages2md.pipeline import _has_embedded_coverage_gap
from pages2md.native import _preserves_supported_text, _source_supported_page, _salvage_grounded_body, _merge_supported_spans, parse_native_observation
from pages2md.quality import math_syntax_errors
from copy import deepcopy
import pytest


def evidence():
    lines = [
        {"text": "The missing opening paragraph.", "bbox": [100, 100, 900, 125]},
        {"text": "The covered continuation paragraph.", "bbox": [100, 200, 900, 225]},
    ]
    return EmbeddedEvidence(text=" ".join(line["text"] for line in lines),
                            blocks=[{**line, "lines": [line]} for line in lines])


def test_large_box_does_not_hide_missing_prose():
    assert _has_embedded_coverage_gap([
        Block("paragraph", "The covered continuation paragraph.", bbox=(50, 50, 950, 900))
    ], evidence())


def test_full_prose_in_large_box_is_covered():
    assert not _has_embedded_coverage_gap([
        Block("paragraph", evidence().text, bbox=(50, 50, 950, 900))
    ], evidence())


def test_text_at_wrong_location_does_not_cover_missing_region():
    assert _has_embedded_coverage_gap([
        Block("paragraph", evidence().text, bbox=(100, 195, 900, 230))
    ], evidence())


def test_local_selection_cannot_drop_supported_opening():
    base = Block("paragraph", evidence().text, bbox=(100, 100, 900, 300))
    partial = Block("paragraph", "The covered continuation paragraph.", bbox=base.bbox)
    assert not _preserves_supported_text(base, [partial], evidence())
    assert _preserves_supported_text(base, [base], evidence())


def test_math_only_abstains_from_prose_veto():
    base = Block("formula", r"\(x_i^2 + y_j = 0\)", bbox=(100, 100, 900, 300))
    assert _preserves_supported_text(base, [], evidence())


def test_ligatures_and_flattened_math_are_not_missing_prose():
    text = "By deﬁnition degX(Q) and degY(Q) are bounded."
    native = EmbeddedEvidence(blocks=[{"lines": [{"text": text, "bbox": [100,100,900,125]}]}])
    visual = Block("paragraph", r"By definition \(\deg_X(Q)\) and \(\deg_Y(Q)\) are bounded.", bbox=(100,100,900,125))
    assert not _has_embedded_coverage_gap([visual], native)


def test_whole_page_score_cannot_buy_a_missing_region():
    primary = parse_native_observation(
        "unrelated hallucinated beginning " * 10
        + "<|det|>text [100,100,900,125]<|/det|>The missing opening paragraph.",
        mode="multi_base", source_pages=[1],
    )
    detail = parse_native_observation(
        "<|det|>text [100,200,900,225]<|/det|>The covered continuation paragraph."
        "<|det|>text [100,250,900,275]<|/det|>Another fully source supported continuation paragraph.",
        mode="gundam_detail", source_pages=[1],
    )
    native = evidence()
    native.text += " Another fully source supported continuation paragraph."
    line = {"text": "Another fully source supported continuation paragraph.", "bbox": [100,250,900,275]}
    native.blocks.append({**line, "lines": [line]})
    assert _source_supported_page(primary, [detail], native) is None
    primary.blocks = primary.blocks[:1]
    assert _source_supported_page(primary, [detail], native) is detail


def test_short_unsupported_prefix_chain_preserves_raw():
    prefix = "2D-" * 12 + "2"
    raw = prefix + "<|det|>text [100,100,900,125]<|/det|>The missing opening paragraph." + "<|det|>text [100,200,900,225]<|/det|>The covered continuation paragraph."
    observation = parse_native_observation(raw, mode="multi_base", source_pages=[1])
    repaired, actions = _salvage_grounded_body(observation, evidence())
    assert repaired.raw == raw
    assert len(repaired.blocks) == 2
    assert actions[0]["action"] == "excluded_unsupported_prefix"
    assert actions[0]["primary_observation"] == observation.id
    assert actions[0]["recovery_observation"] == observation.id
    supported = evidence()
    supported.text = prefix + " " + supported.text
    unchanged, actions = _salvage_grounded_body(observation, supported)
    assert len(unchanged.blocks) == 3
    assert not actions


def test_local_splice_cannot_unbalance_math_braces():
    base = r"construction from [CS25] and \(\mathrm{[BCH^{+}25]}\)."
    detail = r"construction from [CS25] and \([\mathrm{BCH}^{+}25]\)."
    merged, _ = _merge_supported_spans(base, detail, "construction from [CS25] and [BCH+25].")
    assert not math_syntax_errors(base)
    assert not math_syntax_errors(detail)
    assert not math_syntax_errors(merged)


@pytest.mark.parametrize("figure", ["missing", "wrong_position", "retained"])
@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("kind", ["figure", "formula", "table"])
def test_page_recovery_preserves_nonprose_region(figure, truncated, kind):
    from pages2md.native import reconcile_observations

    primary = parse_native_observation(
        "<|det|>text [100,100,900,125]<|/det|>Unrelated hallucinated prose words totally wrong."
        "<|det|>figure [100,400,900,700]<|/det|>",
        mode="multi_base", source_pages=[1],
        generation={"finish_reason": "length" if truncated else "stop"},
    )
    primary.blocks[-1].kind = kind
    primary.blocks[-1].markdown = {
        "figure": "", "formula": r"\(x^2+y^2=z^2\)",
        "table": "<table><tr><td>123</td><td>456</td></tr></table>",
    }[kind]
    detail = parse_native_observation(
        "<|det|>text [100,100,900,125]<|/det|>The missing opening paragraph."
        "<|det|>text [100,200,900,225]<|/det|>The covered continuation paragraph.",
        mode="gundam_detail", source_pages=[1], generation={"finish_reason": "stop"},
    )
    native = evidence()
    for source, block in zip(native.blocks, detail.blocks):
        source["text"] += " Additional reliable prose supplies enough source context for the whole page score."
        source["lines"][0]["text"] = source["text"]
        block.markdown = source["text"]
    native.text = " ".join(b["text"] for b in native.blocks)
    if figure != "missing":
        copied = deepcopy(primary.blocks[-1])
        if figure == "wrong_position":
            copied.bbox = (100,750,900,950)
        detail.blocks.append(copied)
    before = deepcopy(primary)
    winner = _source_supported_page(primary, [detail], native)
    assert (winner is detail) == (figure == "retained")
    blocks, _, _ = reconcile_observations(primary, [detail], embedded=native)
    assert any(b.kind == kind and b.bbox == primary.blocks[-1].bbox for b in blocks)
    assert primary == before


@pytest.mark.parametrize("ink_y, blank_expected", [(2927, True), (2928, False)])
def test_blank_region_does_not_borrow_adjacent_line_pixel(tmp_path, ink_y, blank_expected):
    from PIL import Image
    from pages2md.native import _blank_nonprose_regions

    primary = parse_native_observation(
        r"<|det|>equation [465,887,531,903]<|/det|>\[T\subseteq S\]",
        mode="multi_base", source_pages=[16],
    )
    image = Image.new("L", (2550,3300), 255)
    image.putpixel((1351,ink_y), 254)
    path = tmp_path/"page.png"
    image.save(path)
    assert bool(_blank_nonprose_regions(primary, path)) == blank_expected


@pytest.mark.parametrize("pixel", [255, 254, 0])
def test_only_white_raster_regions_can_waive_preservation(tmp_path, pixel):
    from PIL import Image
    from pages2md.native import _blank_nonprose_regions, _preserves_nonprose_regions

    primary = parse_native_observation(
        r"<|det|>equation [100,400,900,700]<|/det|>\[x=1\]",
        mode="multi_base", source_pages=[1],
    )
    recovery = deepcopy(primary)
    recovery.blocks = []
    image = Image.new("L", (1000,1000), 255)
    image.putpixel((500,500), pixel)
    path = tmp_path/"page.png"
    image.save(path)
    before = deepcopy(primary)
    blank = _blank_nonprose_regions(primary, path)
    assert bool(blank) == (pixel == 255)
    assert _preserves_nonprose_regions(primary, recovery, blank) == (pixel == 255)
    assert not _preserves_nonprose_regions(primary, recovery)
    assert not _blank_nonprose_regions(primary, None)
    assert primary == before
