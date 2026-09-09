from types import SimpleNamespace

import pytest

from pages2md.block_decoding import (BlockLogitsProcessor, ReplayBranchProcessor,
                                    duplicate_blocks, fingerprint, region_coverage, valid_header_prefix)
from pages2md.ocr import MlxUnlimitedOcr


@pytest.mark.parametrize("text", ["", "text", "text [", "text [100, 20, 9", "text [100,20,900,1000]",
                                        "equation [0,0,1,1]<|/de", "caption [0,0,1000,1000]<|/det|>body",
                                        "text [999,999,1000,1000]<|/det|>"])
def test_grammar_allows_valid_partial_headers(text):
    assert valid_header_prefix(text)


@pytest.mark.parametrize("text", ["text [-", "text [1001", "text [0,-371", "text [100,20,100,",
                                        "text [100,20,90,", "text [0,0,1,0]", "text [0,0,1,1]<PAGE>",
                                        "text [1000", "text [1,1000"])
def test_grammar_rejects_invalid_coordinates_and_markers(text):
    assert not valid_header_prefix(text)


def region(box, body, kind="equation"):
    return f"<|det|>{kind} [{','.join(map(str, box))}]<|/det|>" + body


BODY = r"\[\leq\sum_{i=1}^{n}\Pr[A_i(x)=0]+\Pr[B_i(x)=1]+\Pr[C_i(x)=2]+\Pr[D_i(x)=3]+\Pr[E_i(x)=4]+\Pr[F_i(x)=5]+\Pr[G_i(x)=6]+\Pr[H_i(x)=7]\]"


def test_near_copy_needs_geometry_and_spares_tables_and_real_derivations():
    first = region((100,100,900,200), BODY)
    second = BODY.replace("F_i", "G_i")
    bad = first + region((100,202,300,218), second)
    assert duplicate_blocks(bad)[0]["reason"] == "compressed_near_copy"
    assert duplicate_blocks(first + region((100,100,900,200), second))
    assert not duplicate_blocks(first + region((100,202,900,302), second))
    assert not duplicate_blocks(first + "<PAGE>" + region((100,100,900,200), second))
    assert not duplicate_blocks(region((100,100,900,200), BODY, "table") * 2)
    assert not duplicate_blocks(first + region((100,202,300,218), r"\[\leq n\epsilon\]"))


def test_fingerprints_preserve_symbol_values_case_and_script_identity():
    assert fingerprint(r"\mathbf{X}_{12}") == fingerprint("X _ {12}")
    assert len({fingerprint(t) for t in ("X_12", "x_12", "X_21", "X^12")}) == 4


def test_region_coverage_tolerates_rounding_but_not_missing_content():
    assert region_coverage((280,464,413,480), (280,466,412,482)) == 1
    assert region_coverage((280,464,413,480), (280,510,413,530)) == 0
    assert region_coverage((100,100,900,200), (100,100,120,110)) < .01


class Tokenizer:
    eos_token_id = 0
    def encode(self, text, **kwargs):
        return list(text.encode("utf-8"))
    def decode(self, ids, **kwargs):
        return bytes(ids).decode("utf-8", errors="replace")


def test_mlx_grammar_selects_legal_number_not_negative_and_keeps_bans():
    import mlx.core as mx
    p = BlockLogitsProcessor(Tokenizer())
    p(mx.array([1]), mx.zeros((1,256)))
    header = list(b"<|det|>text [100,")
    logits = mx.full((1,256), -float("inf"))
    logits[0,ord("-")], logits[0,ord("3")] = 10, 5
    output = p(mx.array([1, *header]), logits)
    assert output[0,ord("-")].item() == -float("inf")
    assert output[0,ord("3")].item() == 5
    assert output[0,ord("4")].item() == -float("inf")
    with pytest.raises(ValueError, match="rollback"):
        p(mx.array([1]), logits)


def test_live_soft_penalty_is_limited_to_a_reused_region():
    import mlx.core as mx
    body = r"\[" + "+".join(f"X_{{{i}}}" for i in range(40)) + r"\]"
    partial = body[:body.index("X_{30}")]
    for box, penalized in [((100,202,300,218), True), ((100,202,900,302), False)]:
        text = region((100,100,900,200), body) + region(box, partial)
        text += " " * (-len(text) % 8)
        p = BlockLogitsProcessor(Tokenizer())
        logits = mx.zeros((1,256))
        logits[0,ord("X")], logits[0,ord("Z")] = 3, 2
        p(mx.array([1]), logits)
        output = p(mx.array([1,*text.encode()]), logits)
        assert output[0,ord("X")].item() == (1 if penalized else 3)
        assert output[0,ord("Z")].item() == 2


def test_replay_rebuilds_prefix_then_excludes_only_explored_branch():
    import mlx.core as mx
    p = ReplayBranchProcessor([65,66], {67})
    logits = mx.zeros((1,128))
    assert mx.isfinite(p(mx.array([1]), logits)).sum().item() == 1
    assert mx.argmax(p(mx.array([1,65]), logits)).item() == 66
    output = p(mx.array([1,65,66]), logits)
    assert output[0,67].item() == -float("inf")
    assert output[0,68].item() == 0
    assert mx.isfinite(p(mx.array([1,65,66,68]), logits)).all().item()


def test_block_policy_is_automatic_single_page_and_identity_is_explicit():
    b = MlxUnlimitedOcr()
    b._processor = Tokenizer()
    assert b.identity["block_retries"] == "2"
    assert any(isinstance(p, BlockLogitsProcessor) for p in b._decode_processors(128, []))
    assert not any(isinstance(p, BlockLogitsProcessor) for p in b._decode_processors(128, [], single_page=False))
    import inspect
    assert set(inspect.signature(MlxUnlimitedOcr).parameters) == {"max_tokens"}


@pytest.mark.parametrize("outcome", ["improved", "unchanged", "error", "missing_region", "new_math_error",
                                     "low_likelihood", "truncated", "changed_prefix"])
def test_bounded_retry_preserves_raw_attempts_and_omits_forced_confidence(monkeypatch, outcome):
    first = region((100,100,900,200), BODY)
    short = region((100,202,300,218), r"\[\leq n\epsilon\]")
    footer = region((490,900,510,920), "45", "page_number")
    original = first + region((100,202,300,218), BODY.replace("F_i", "G_i")) + short + footer
    corrected = first + short + footer
    backend = MlxUnlimitedOcr()
    tokenizer = Tokenizer()
    factory = lambda: [BlockLogitsProcessor(tokenizer)]
    calls = []

    def stream(**kwargs):
        processor = next(p for p in kwargs["logits_processors"] if isinstance(p, BlockLogitsProcessor))
        index = len(calls)
        calls.append(kwargs)
        if index and outcome == "error":
            raise ValueError("synthetic hard-constraint conflict")
        raw = corrected if index and outcome != "unchanged" else original
        if index and outcome == "missing_region":
            raw = raw.removesuffix(footer)
        if index and outcome == "new_math_error":
            raw = raw.replace(r"n\epsilon", r"n_{\epsilon")
        if index and outcome == "changed_prefix":
            raw = raw.replace("A_i", "Z_i", 1)
        processor.text = raw
        processor.ids = tokenizer.encode(raw)
        processor.ends = list(range(1, len(processor.ids) + 1))
        processor.model_scores = [-3 if index and outcome == "low_likelihood" else -.1] * len(processor.ids)
        finish = "length" if index and outcome == "truncated" else "stop"
        return SimpleNamespace(text=raw, finish_reason=finish, generation_tokens=len(processor.ids)), {
            "summary": {"mean": .9}, "spans": [{"confidence": .9}]}

    monkeypatch.setattr(backend, "_stream_with_confidence", stream)
    result, confidence = backend._generate_with_confidence(_retry_factory=factory,
                                                           logits_processors=factory(), max_tokens=4096)
    retry = confidence["decoding"]["block_retry"]
    assert retry["attempts"][0]["raw"] == original
    assert 2 <= len(calls) <= 3
    assert len({id(next(p for p in call["logits_processors"] if isinstance(p, BlockLogitsProcessor))) for call in calls}) == len(calls)
    if outcome == "improved":
        assert result.text == corrected
        assert retry["selected_attempt"] == 1
        assert confidence["summary"] is None and confidence["spans"] == []
    else:
        assert result.text == original and retry["selected_attempt"] == 0
        assert confidence["summary"] == {"mean": .9}
        if outcome == "error":
            assert "error" in retry["attempts"][1]


def test_short_and_separate_repetitions_are_not_duplicate_blocks():
    short = "The same observation is recorded independently."
    assert not duplicate_blocks(region((100,100,900,120), short, "text") * 2)
    paragraph = "Each independent trial retains its original measurement. " * 4
    assert not duplicate_blocks(region((100,100,900,220), paragraph, "text") +
                                region((100,500,900,620), paragraph, "text"))


def test_header_grammar_widens_past_top32_and_does_not_release_eos():
    import mlx.core as mx
    tokenizer = Tokenizer()
    processor = BlockLogitsProcessor(tokenizer)
    logits = mx.zeros((1,256))
    processor(mx.array([1]), logits)
    logits[0,128:192] = 5  # incomplete UTF-8 bytes cannot begin a label
    logits[0,ord("A")] = 1
    output = processor(mx.array([1,*b"<|det|>"]), logits)
    assert output[0,0].item() == -float("inf")
    assert mx.isfinite(output[0,ord("A")]).item()


def test_replay_never_overrides_an_existing_hard_mask():
    import mlx.core as mx
    processor = ReplayBranchProcessor([65], set())
    logits = mx.zeros((1,128))
    logits[0,65] = -float("inf")
    with pytest.raises(ValueError, match="conflicts"):
        processor(mx.array([1]), logits)


@pytest.mark.parametrize("outcome", ["loop", "short", "length", "grounded", "coherent",
                                     "error", "unhelpful", "truncated", "bad_marker", "footer_only", "page_number_only"])
def test_startup_recovery_is_bounded_and_does_not_perturb_usable_bodies(monkeypatch, outcome):
    from pages2md.decoding import InitialGroundingProcessor
    tokenizer = Tokenizer()
    if outcome == "bad_marker":
        original_encode = tokenizer.encode
        tokenizer.encode = lambda text, **kw: [] if text == "<|det|>" else original_encode(text, **kw)
    backend = MlxUnlimitedOcr()
    corrected = region((100,100,900,200), "Recovered source paragraph.", "text")
    original = "repeated loop " * 30
    if outcome == "short":
        original = ""
    if outcome == "grounded":
        original = "Bad prefix " * 40 + corrected
    if outcome == "coherent":
        original = " ".join(f"Word{i} distinct clause." for i in range(40))
    if outcome == "footer_only":
        original = "Ungrounded invented explanation of a completely different topic. " * 3 + region((490,900,510,920), "1", "page_number")
    if outcome == "page_number_only":
        original = region((490,900,510,920), "1", "page_number")
    calls = []
    factory = lambda: [BlockLogitsProcessor(tokenizer)]

    def stream(**kwargs):
        index = len(calls)
        calls.append(kwargs)
        if index:
            assert any(isinstance(p, InitialGroundingProcessor) for p in kwargs["logits_processors"])
        if index and outcome == "error":
            raise ValueError("synthetic constraint conflict")
        raw = corrected if index and outcome != "unhelpful" else original
        tracker = next(p for p in kwargs["logits_processors"] if isinstance(p, BlockLogitsProcessor))
        tracker.text = raw
        tracker.ids = tokenizer.encode(raw)
        tracker.ends = list(range(1, len(tracker.ids)+1))
        tracker.model_scores = [-.1] * len(tracker.ids)
        finish = "length" if (outcome == "length" and not index) or (outcome == "truncated" and index) else "stop"
        return SimpleNamespace(text=raw, generation_tokens=len(tracker.ids), finish_reason=finish), {"summary": None, "spans": []}

    monkeypatch.setattr(backend, "_stream_with_confidence", stream)
    result, confidence = backend._generate_with_confidence(_retry_factory=factory, logits_processors=factory())
    if outcome in {"grounded", "coherent", "page_number_only"}:
        assert len(calls) == 1 and result.text == original
        assert "startup_retry" not in confidence["decoding"]
    else:
        assert len(calls) == (1 if outcome == "bad_marker" else 2)
        retry = confidence["decoding"]["startup_retry"]
        assert retry["attempts"][0]["raw"] == original
        selected = outcome in {"loop", "short", "length", "footer_only"}
        assert retry["selected_attempt"] == int(selected)
        assert result.text == (corrected if selected else original)


@pytest.mark.parametrize("grounded", [False, True])
def test_startup_budget_does_not_cap_grounded_long_pages(monkeypatch, grounded):
    import sys
    closed = []
    backend = MlxUnlimitedOcr()
    backend._processor = Tokenizer()

    def stream_generate(**kwargs):
        try:
            for i in range(1, 4301):
                text = region((100,100,900,200), "Source text.", "text") if i == 1 and grounded else f"Unique{i} "
                yield SimpleNamespace(text=text, generation_tokens=i, token=65, logprobs=None,
                                      prompt_tokens=3, peak_memory=0, finish_reason="stop" if i == 4300 else None)
        finally:
            closed.append(True)

    monkeypatch.setitem(sys.modules, "mlx_vlm", SimpleNamespace(stream_generate=stream_generate))
    result, _ = backend._stream_with_confidence(logits_processors=[BlockLogitsProcessor(backend._processor)])
    assert result.generation_tokens == (4300 if grounded else 4096)
    assert result.finish_reason == ("stop" if grounded else "ungrounded_guard")
    assert closed == [True]
    if not grounded:
        from pages2md.native import parse_native_observation
        observation = parse_native_observation(result.text, mode="multi_base", source_pages=[1],
                                               generation={"finish_reason": result.finish_reason})
        assert "visual_truncated" in observation.warnings
