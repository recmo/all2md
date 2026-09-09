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
def test_page_recovery_preserves_detected_figure(figure, truncated):
    from pages2md.native import reconcile_observations

    primary = parse_native_observation(
        "<|det|>text [100,100,900,125]<|/det|>Unrelated hallucinated prose words totally wrong."
        "<|det|>figure [100,400,900,700]<|/det|>",
        mode="multi_base", source_pages=[1],
        generation={"finish_reason": "length" if truncated else "stop"},
    )
    detail = parse_native_observation(
        "<|det|>text [100,100,900,125]<|/det|>The missing opening paragraph."
        "<|det|>text [100,200,900,225]<|/det|>The covered continuation paragraph.",
        mode="gundam_detail", source_pages=[1], generation={"finish_reason": "stop"},
    )
    if figure != "missing":
        copied = deepcopy(primary.blocks[-1])
        if figure == "wrong_position":
            copied.bbox = (100,750,900,950)
        detail.blocks.append(copied)
    before = deepcopy(primary)
    winner = _source_supported_page(primary, [detail], evidence())
    assert (winner is detail) == (figure == "retained")
    blocks, _, _ = reconcile_observations(primary, [detail], embedded=evidence())
    assert any(b.kind == "figure" and b.bbox == primary.blocks[-1].bbox for b in blocks)
    assert primary == before
