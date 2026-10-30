from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from pages2md.decoding import (DecodeGuide, DecodeLogitsProcessor, InitialGroundingProcessor,
                              SourceProgress, structural_loop, words)
from pages2md.model import EmbeddedEvidence, SourcePage
from pages2md.native import parse_native_observation, reconcile_observations
from pages2md.ocr import MlxUnlimitedOcr
from pages2md.pipeline import _compatible_ocr_fingerprint, _recognize_with_evidence
from pages2md.quality import unresolved_decode_errors


def evidence(*paragraphs):
    blocks = []
    for i, text in enumerate(paragraphs):
        y = 100 + 150 * i
        blocks.append({"text": text, "bbox": [100, y, 900, y + 80], "lines": [{"spans": [{
            "chars": [{"text": c, "bbox": [110 + j * 2, y + 10, 112 + j * 2, y + 30]}
                      for j, c in enumerate(text)], "font": "Times", "size": 10,
        }]}]})
    return EmbeddedEvidence(text="\n".join(paragraphs), blocks=blocks, extractor="pymupdf")


@pytest.mark.parametrize("text", [
    " ".join(rf"\( \alpha_{{{i}}} \)" for i in range(1, 80)),
    " ".join(f"(1/2) ({i})" for i in range(1, 80)),
    "The case of Delta is the case of Delta. " * 20,
    " ".join(f"1.1.{i}" for i in range(80)),
])
def test_structural_detector_catches_reviewed_loop_families(text):
    assert structural_loop(text)


@pytest.mark.parametrize("text", [
    " ".join(rf"\( \alpha_{{{i}}} \)" for i in range(1, 5)),
    "<table>" + "".join(f"<tr><td>{i}</td><td>0</td></tr>" for i in range(100)) + "</table>",
    "\n".join("|0|0|0|" for _ in range(100)),
    "\n".join(f"![Panel {i}](figure.png)" for i in range(100)),
    "<|det|>text [100,100,800,800]<|/det|>" * 100,
])
def test_structural_detector_spares_short_formulas_tables_and_markup(text):
    assert not structural_loop(text)


def test_counter_projection_does_not_change_source_values():
    assert words(r"\alpha_{12} + \alpha_{21}") == ["α", "12", "α", "21"]
    assert words("α12 + α21") == ["α", "12", "α", "21"]


def test_source_occurrences_are_consumed_and_not_reused():
    source = SourceProgress([evidence("The same phrase occurs. The same phrase ends.")])
    for word in words("The same phrase occurs. The same phrase ends."):
        assert source.consume(word)
    assert source.matched == 8
    assert source.prefix_score("the") == 0
    assert not source.consume("the")


def test_page_and_geometry_select_local_evidence():
    source = SourceProgress([evidence("First source paragraph here.", "Second distinct paragraph here."),
                             evidence("Third page follows later.")])
    source.select_box((100, 250, 900, 330))
    assert source.prefix_score("second") > 0
    assert source.prefix_score("first") == 0
    assert source.prefix_score("third") == 0
    source.page = 1
    source.active = None
    assert source.prefix_score("third") > 0


def test_unusable_native_regions_abstain_independently():
    native = evidence("Reliable paragraph remains useful.", "Broken paragraph must not guide.")
    native.blocks[1]["lines"][0]["spans"][0]["chars"][0]["bbox"] = [0, 0, 0, 0]
    assert len(SourceProgress([native]).regions) == 1
    assert not SourceProgress([EmbeddedEvidence(text="No geometry here.")]).regions
    native.extractor = "ignored"
    assert not SourceProgress([native]).regions


def test_positive_bias_is_source_progress_not_rejection():
    guide = DecodeGuide([evidence("Interleaving polynomial generators improves decoding.")])
    guide.feed("<|det|>text [100,100,900,180]<|/det|>")
    assert guide.score("Interleaving") > guide.score("nonsense")
    assert guide.source.matched == 0  # candidate scoring is read-only
    guide.feed("Interleaving polynomial generators ")
    assert guide.source.matched == 3
    assert guide.score("improves") > guide.score("Interleaving")


def test_leading_space_bpe_candidate_advances_hypothetical_word_only():
    guide = DecodeGuide([evidence("First source paragraph begins here.")])
    guide.feed("<|det|>text [100,100,900,180]<|/det|>First")
    assert guide.score(" source") > guide.score(" nonsense")
    assert guide.source.matched == 0
    assert guide.source.regions[0].cursor == 0
    guide.feed(" source")
    assert guide.source.matched == 1
    assert guide.score(" paragraph") > 0


def test_positive_bias_does_not_flatten_or_skip_inline_math():
    guide = DecodeGuide([evidence("Fix a field F and a parameter n for this theorem.")])
    guide.feed("<|det|>text [100,100,900,180]<|/det|>Fix a field")
    assert guide.score(" F") == 0
    assert guide.score(" and") == 0
    assert guide.score(r" \(\mathbb{F}\)") == 0
    guide.feed(r" \(\mathbb{F}\) and a parameter \(n\) for ")
    assert guide.score("this") > 0


def test_split_grounding_and_latex_commands_never_consume_source_words():
    guide = DecodeGuide([evidence("Actual paragraph with reliable source words.")])
    for part in ["<|de", "t|>text [100,100,900,180]<|/", "det|>"]:
        guide.feed(part)
    assert guide.source.matched == 0
    assert guide.source.active == 0
    guide.feed("Actual para")
    assert guide.score("graph") > guide.score("site")
    guide.feed("graph ")
    assert guide.source.matched == 2
    guide.feed(r"\(\frac{1}{2}\) ")
    assert guide.source.matched == 2


def test_unmatched_grounded_region_does_not_borrow_page_prose():
    guide = DecodeGuide([evidence("Actual paragraph with reliable source words.")])
    guide.feed("<|det|>image [0,700,500,990]<|/det|>")
    assert guide.score("Actual") == 0


def test_eos_is_soft_and_only_discouraged_with_active_source_progress():
    guide = DecodeGuide([evidence("One two three four five six seven eight nine ten eleven twelve thirteen.")])
    guide.feed("<|det|>text [100,100,900,180]<|/det|>")
    assert guide.score("", eos=True) == 0
    guide.feed("One two three ")
    assert -2 < guide.score("", eos=True) < 0
    guide.feed("unrelated " * 10)
    assert guide.score("", eos=True) == 0


def test_structural_penalty_steers_away_from_loop():
    guide = DecodeGuide([])
    guide.feed("loop phrase " * 80)
    assert guide.score("loop ") < guide.score("Conclusion ")


def test_math_occurrences_corroborate_without_flattened_math_bias():
    native = evidence(" ".join(f"α{i}" for i in range(1, 40)))
    guide = DecodeGuide([native])
    guide.feed("<|det|>equation [100,100,900,180]<|/det|>")
    for i in range(1, 30):
        guide.feed(rf"\(\alpha_{{{i}}}\) ")
    assert guide.source.matched == 58
    assert guide.recent_source_progress
    assert guide.score(r"\(") == 0
    assert not guide.source.frontier(steering=True)


def test_short_self_recovering_prefix_is_not_perturbed():
    guide = DecodeGuide([evidence("Actual source paragraph remains useful.")])
    guide.feed(r"\(\mu\) = " * 6)
    assert guide.score("=") == 0
    assert guide.score("Actual") == 0
    assert guide.source.matched == 0


def test_unsupported_prefix_salvage_preserves_raw_and_source_supported_title():
    title, body = "Interleaving polynomial generators", "This abstract contains the actual source page content."
    native = evidence(title, body)
    primary = parse_native_observation("unrelated short output", mode="multi_base", source_pages=[1])
    raw = "nonsense parameters " * 20 + f"<|det|>title [100,100,900,180]<|/det|>{title}" \
        + f"<|det|>text [100,250,900,330]<|/det|>{body}"
    detail = parse_native_observation(raw, mode="gundam_detail", source_pages=[1],
                                      generation={"target_block_indices": [], "finish_reason": "stop"})
    original = deepcopy(detail)
    blocks, actions, warnings = reconcile_observations(primary, [detail], embedded=native)
    assert [b.markdown for b in blocks] == [title, body]
    assert detail == original
    assert detail.raw == raw
    assert {a["action"] for a in actions} == {"excluded_unsupported_prefix", "selected_source_supported_page"}
    assert "visual_text_repetition" not in warnings


def test_no_whole_page_replacement_from_untrusted_or_wrong_page_evidence():
    native = evidence("Actual source paragraph remains.", "Another source paragraph follows.")
    primary = parse_native_observation("Short original", mode="multi_base", source_pages=[1])
    raw = "<|det|>text [100,100,900,180]<|/det|>Actual source paragraph remains." \
        + "<|det|>text [100,250,900,330]<|/det|>Another source paragraph follows."
    recovery = parse_native_observation(raw, mode="gundam_detail", source_pages=[2])
    _, actions, _ = reconcile_observations(primary, [recovery], embedded=native)
    assert not any(a["action"] == "selected_source_supported_page" for a in actions)


def test_empty_primary_never_adopts_a_runaway_recovery():
    primary = parse_native_observation("", mode="multi_base", source_pages=[1])
    recovery = parse_native_observation("loop phrase " * 1000, mode="gundam_detail", source_pages=[1])
    recovery.warnings = []  # simulate a checkpoint predating current validators
    blocks, _, warnings = reconcile_observations(primary, [recovery])
    assert blocks == []
    assert "visual_empty_output" in warnings


def test_numeric_hierarchy_prefix_is_removed_but_preserved_in_raw():
    native = evidence("Actual source paragraph remains.", "Another source paragraph follows.")
    prefix = "1." * 40
    raw = prefix + "<|det|>text [100,100,900,180]<|/det|>Actual source paragraph remains." \
        + "<|det|>text [100,250,900,330]<|/det|>Another source paragraph follows."
    primary = parse_native_observation(raw, mode="multi_base", source_pages=[1])
    blocks, actions, _ = reconcile_observations(primary, [], embedded=native)
    assert len(blocks) == 2
    assert primary.raw.startswith(prefix)
    assert actions[0]["action"] == "excluded_unsupported_prefix"


def test_verification_distinguishes_rejected_attempts_from_remaining_failure():
    loop = "loop phrase " * 80
    assert unresolved_decode_errors({"visual_markdown": loop})
    assert not unresolved_decode_errors({"visual_markdown": "Recovered source content.",
                                         "warnings": ["visual_structural_repetition"]})
    assert not unresolved_decode_errors({"visual_markdown": loop, "embedded": {"text": loop}})


def test_guidance_fingerprints_preserve_old_evidence_but_enforce_image_only():
    old = {"backend": {"revision": "1"}, "contract_version": 1}
    guided = {**old, "embedded_decode": True}
    assert _compatible_ocr_fingerprint(old, guided)
    assert _compatible_ocr_fingerprint(guided, guided)
    assert not _compatible_ocr_fingerprint(guided, old)
    assert not _compatible_ocr_fingerprint(old, {**guided, "contract_version": 2})


def test_automatic_decoder_reuses_old_raw_without_relaxing_source_model_or_native_policy():
    current = {"backend": MlxUnlimitedOcr().identity, "source_sha256": "source", "dpi": 300,
               "embedded_decode": True, "contract_version": 1}
    old = deepcopy(current)
    for key in ("startup_recovery", "block_decoding", "block_retries"):
        old["backend"].pop(key)
    snapshot = deepcopy(old)
    assert _compatible_ocr_fingerprint(old, current)
    assert old == snapshot
    for key, value in [("revision", "different-model"), ("max_tokens", "8192"), ("vision_precision", "float16")]:
        changed = deepcopy(current)
        changed["backend"][key] = value
        assert not _compatible_ocr_fingerprint(old, changed)
    assert not _compatible_ocr_fingerprint(old, {**current, "source_sha256": "changed"})
    assert not _compatible_ocr_fingerprint(old, {**current, "dpi": 150})
    assert not _compatible_ocr_fingerprint(old, {**current, "embedded_decode": False})


def test_backend_evidence_is_per_invocation():
    page = SourcePage(1, Path("one.png"), evidence("Reliable source text here."))
    seen = []
    backend = SimpleNamespace(recognize_pages=lambda images, **kw: seen.append((images, kw)))
    _recognize_with_evidence(backend, "recognize_pages", [page])
    assert seen == [([page.image_path], {"embedded": [page.embedded]})]


class CharacterTokenizer:
    eos_token_id = 0
    eos_token_ids = 1  # production tokenizer exposes this plural property as int

    def encode(self, text, **kwargs):
        return [ord(c) for c in text]

    def decode(self, ids, **kwargs):
        return "".join(chr(i) for i in ids if i)


def test_initial_grounding_forces_only_marker_and_then_releases_all_tokens():
    import mlx.core as mx
    processor = InitialGroundingProcessor(CharacterTokenizer())
    prefill = [10, 11, 12]
    logits = mx.arange(128, dtype=mx.float32)[None, :]
    for offset, token in enumerate(processor.prefix):
        output = processor(mx.array(prefill + processor.prefix[:offset]), logits)
        assert mx.isfinite(output).sum().item() == 1
        assert output[0, token].item() == logits[0, token].item()
    history = prefill + processor.prefix
    assert processor(mx.array(history), logits) is logits
    assert processor(mx.array(history + [65, 66]), logits) is logits
    with pytest.raises(ValueError, match="rollback"):
        processor(mx.array(history + [65]), logits)


def test_initial_grounding_rejects_invalid_tokenizer_batch_and_conflicting_mask():
    import mlx.core as mx
    with pytest.raises(ValueError, match="round-trip"):
        InitialGroundingProcessor(SimpleNamespace(encode=lambda *a, **k: []))
    processor = InitialGroundingProcessor(CharacterTokenizer())
    with pytest.raises(ValueError, match="one decoding hypothesis"):
        processor(mx.array([[1], [2]]), mx.zeros((2, 128)))
    with pytest.raises(ValueError, match="hard constraint"):
        processor(mx.array([1]), mx.full((1, 128), -float("inf")))


def test_initial_grounding_is_only_added_during_recovery():
    backend = MlxUnlimitedOcr()
    backend._processor = CharacterTokenizer()
    processors = backend._decode_processors(128, [])
    assert not any(isinstance(p, InitialGroundingProcessor) for p in processors)
    processors.append(InitialGroundingProcessor(backend._processor))
    assert backend._decode_diagnostics(processors)["forced_prefix_tokens"] == len("<|det|>")



def test_mlx_adapter_can_promote_source_outside_topk_and_keeps_hard_masks():
    import mlx.core as mx
    p = DecodeLogitsProcessor(CharacterTokenizer(), [evidence("Zebra source paragraph begins here.")])
    assert p.eos_ids == {0, 1}
    p.guide.feed("<|det|>text [100,100,900,180]<|/det|>")
    logits = mx.zeros((1, 128), dtype=mx.bfloat16)
    logits[0, ord("z")] = -.1
    logits[0, ord("Z")] = -float("inf")
    out = p(mx.array([ord("p"), ord("r")]), logits)  # ignored prefill
    assert out.dtype == mx.float32
    assert out[0, ord("z")].item() > 0
    assert out[0, ord("Z")].item() == -float("inf")
    assert p.guide.source.matched == 0
    p(mx.array([ord("p"), ord("r"), ord("Z"), ord(" ")]), logits)
    assert p.guide.source.emitted == 1
    with pytest.raises(ValueError, match="rollback"):
        p(mx.array([1]), logits)


def test_byte_fallback_is_buffered_until_complete_utf8():
    import mlx.core as mx
    class ByteTokenizer(CharacterTokenizer):
        def encode(self, text, **kwargs):
            return list(text.encode("utf-8"))

        def decode(self, ids, **kwargs):
            return bytes(ids).decode("utf-8", errors="replace")

    p = DecodeLogitsProcessor(ByteTokenizer(), [evidence("é commence un texte stable.")])
    p.guide.feed("<|det|>text [100,100,900,180]<|/det|>")
    logits = mx.zeros((1, 256))
    p(mx.array([2]), logits)
    p(mx.array([2, 195]), logits)
    assert p.pending_ids == [195]
    assert "�" not in p.guide.tail
    p(mx.array([2, 195, 169]), logits)
    assert p.pending_ids == []
    assert p.guide.pending == "é"
    p(mx.array([2, 195, 169, 32]), logits)
    assert p.guide.source.matched == 1


def test_stream_guard_stops_sustained_loop_and_closes_generator(monkeypatch):
    closed = []
    def stream_generate(**kwargs):
        try:
            for i in range(1, 1200):
                yield SimpleNamespace(text="loop phrase ", generation_tokens=i, token=65,
                                      logprobs=None, prompt_tokens=3, peak_memory=0, finish_reason=None)
        finally:
            closed.append(True)
    monkeypatch.setitem(sys.modules, "mlx_vlm", SimpleNamespace(stream_generate=stream_generate))
    backend = MlxUnlimitedOcr()
    backend._processor = CharacterTokenizer()
    result, _ = backend._generate_with_confidence()
    assert result.finish_reason == "repetition_guard"
    assert result.generation_tokens == 768
    assert closed == [True]
    assert result.text.startswith("loop phrase ")


def test_stream_guard_allows_short_bad_prefix_to_recover(monkeypatch):
    def stream_generate(**kwargs):
        for i in range(1, 900):
            yield SimpleNamespace(text="loop phrase " if i < 100 else f"Unique paragraph {i} content. ",
                                  generation_tokens=i, token=65, logprobs=None,
                                  prompt_tokens=3, peak_memory=0, finish_reason="stop" if i == 899 else None)
    monkeypatch.setitem(sys.modules, "mlx_vlm", SimpleNamespace(stream_generate=stream_generate))
    backend = MlxUnlimitedOcr()
    backend._processor = CharacterTokenizer()
    result, _ = backend._generate_with_confidence()
    assert result.finish_reason == "stop"
    assert result.generation_tokens == 899
