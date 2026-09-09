from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import math
import re
from pathlib import Path
from typing import Protocol

from .constants import MODEL_ID, MODEL_REVISION
from .model import Block, EmbeddedEvidence
from .decoding import (DecodeLogitsProcessor, InitialGroundingProcessor, GROUNDING_START_VERSION,
                       POLICY_VERSION, loop_pattern, structural_atoms, structural_loop)
from .block_decoding import (BlockLogitsProcessor, ReplayBranchProcessor, VERSION as BLOCK_POLICY,
                             blocks as grounded_blocks, duplicate_blocks, area, region_coverage)

MULTI_PAGE_PROMPT = "<image>Multi page parsing."
GUNDAM_PROMPT = "<image>document parsing."

DETECTION = re.compile(
    r"<\|det\|>\s*([^\[]+?)\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]\s*<\|/det\|>",
    re.DOTALL,
)


def parse_output(raw: str) -> tuple[str, list[Block]]:
    matches = list(DETECTION.finditer(raw))
    if not matches:
        cleaned = raw.replace("<PAGE>", "").strip()
        return cleaned, [Block(kind="text", markdown=cleaned)] if cleaned else []
    blocks: list[Block] = []
    pieces: list[str] = []
    prefix = raw[: matches[0].start()].strip()
    if prefix:
        pieces.append(prefix)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        content = raw[match.end() : end].replace("<PAGE>", "").strip()
        kind = re.sub(r"[^a-z0-9]+", "_", match.group(1).strip().lower()).strip("_") or "text"
        bbox = tuple(float(match.group(i)) for i in range(2, 6))
        blocks.append(Block(kind=kind, markdown=content, bbox=bbox))
        if content:
            pieces.append(content)
    return "\n\n".join(pieces).strip(), blocks


class OcrBackend(Protocol):
    identity: dict[str, str]
    supports_region_recovery: bool

    def recognize(self, image: Path, *, embedded: EmbeddedEvidence | None = None) -> tuple[str, dict[str, object]]: ...

    def recognize_pages(self, images: list[Path], *, embedded: list[EmbeddedEvidence] | None = None) -> tuple[str, dict[str, object]]: ...

    def recognize_detail(self, image: Path, *, embedded: EmbeddedEvidence | None = None) -> tuple[str, dict[str, object]]: ...

    def recognize_region(self, image: Path) -> tuple[str, dict[str, object]]: ...


class MlxUnlimitedOcr:
    supports_region_recovery = True

    def __init__(self, max_tokens: int = 32768):
        self.max_tokens = max_tokens
        self.precision = {"vision": "float32", "decoder": "bfloat16"}
        self.identity = {
            "engine": "mlx-vlm",
            "model": MODEL_ID,
            "revision": MODEL_REVISION,
            "max_tokens": str(max_tokens),
            "vision_precision": self.precision["vision"],
            "decoder_precision": self.precision["decoder"],
            "startup_recovery": "ungrounded-recovery-v1",
            "block_decoding": BLOCK_POLICY,
            "block_retries": "2",
        }
        self._model = None
        self._processor = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from mlx_vlm import load
        except ImportError as error:
            raise RuntimeError("MLX OCR dependencies are missing; install pages2md[ocr]") from error
        # DeepSeek OCR's processor prints tokenizer-registration details during
        # initialization. Leave stderr untouched so downloads and diagnostics
        # remain visible while suppressing that unconditional stdout noise.
        with redirect_stdout(StringIO()):
            self._model, self._processor = load(MODEL_ID, revision=MODEL_REVISION)
        self._configure_precision()

    def _configure_precision(self) -> None:
        import mlx.core as mx

        for name in ("sam_model", "vision_model", "projector"):
            component = getattr(self._model, name, None)
            if component is not None:
                component.set_dtype(mx.float32)
        for name in ("image_newline", "view_separator"):
            value = getattr(self._model, name, None)
            if value is not None and hasattr(value, "astype"):
                setattr(self._model, name, value.astype(mx.float32))
        language_model = getattr(self._model, "language_model", None)
        if language_model is not None:
            language_model.set_dtype(mx.bfloat16)

        processor = self._processor
        original = getattr(processor, "process_one", None)
        if not callable(original) or getattr(processor, "_pages2md_fp32_images", False):
            return

        def process_one(*args, **kwargs):
            value = original(*args, **kwargs)
            if isinstance(value, dict) and "images" in value:
                value["images"] = _cast_arrays(value["images"], mx.float32)
            return value

        processor.process_one = process_one
        processor._pages2md_fp32_images = True

    def recognize(self, image: Path, *, embedded: EmbeddedEvidence | None = None) -> tuple[str, dict[str, object]]:
        return self._recognize(
            image,
            task=GUNDAM_PROMPT.removeprefix("<image>"),
            cropping=True,
            image_size=640,
            mode="gundam",
            ngram_window=128,
            embedded=embedded,
        )

    def recognize_detail(self, image: Path, *, embedded: EmbeddedEvidence | None = None) -> tuple[str, dict[str, object]]:
        return self._recognize(
            image,
            task=GUNDAM_PROMPT.removeprefix("<image>"),
            cropping=True,
            image_size=640,
            mode="gundam_detail",
            ngram_window=128,
            embedded=embedded,
        )

    def recognize_region(self, image: Path) -> tuple[str, dict[str, object]]:
        """Small visual-only recovery with an internal, bounded token budget."""
        return self._recognize(
            image, task=GUNDAM_PROMPT.removeprefix("<image>"), cropping=True,
            image_size=640, mode="region_detail", ngram_window=128,
            max_tokens=min(self.max_tokens, 4096),
        )

    def recognize_pages(self, images: list[Path], *, embedded: list[EmbeddedEvidence] | None = None) -> tuple[str, dict[str, object]]:
        """Port Unlimited-OCR's `infer_multi` contract to MLX.

        The reference implementation expands every page at one image-token position.
        mlx-vlm requires one marker per image, but with no text between the markers it
        produces the same contiguous image-token sequence.
        """
        if not images:
            raise ValueError("recognize_pages requires at least one image")
        if embedded is not None and len(embedded) != len(images):
            raise ValueError("embedded evidence must match the image count")
        self._load()
        from mlx_vlm.prompt_utils import apply_chat_template

        prompt = apply_chat_template(
            self._processor,
            self._model.config,
            "Multi page parsing.",
            num_images=len(images),
        )
        processors = self._decode_processors(1024, embedded or [], single_page=len(images) == 1)
        result, confidence = self._generate_with_confidence(
            _retry_factory=lambda: self._decode_processors(1024, embedded or [], single_page=len(images) == 1),
            model=self._model,
            processor=self._processor,
            image=[str(image) for image in images],
            prompt=prompt,
            max_tokens=self.max_tokens,
            temperature=0.0,
            cropping=False,
            image_size=1024,
            base_size=1024,
            logits_processors=processors,
        )
        return result.text, {
            "prompt_tokens": result.prompt_tokens,
            "generation_tokens": result.generation_tokens,
            "finish_reason": result.finish_reason,
            "peak_memory_gb": result.peak_memory,
            "mode": "multi_base",
            "group_size": len(images),
            "confidence": confidence["summary"],
            "_confidence_spans": confidence["spans"],
            "decoding": confidence.get("decoding", self._decode_diagnostics(processors)),
            "contract": {
                "prompt": MULTI_PAGE_PROMPT,
                "base_size": 1024,
                "image_size": 1024,
                "crop_mode": False,
                "temperature": 0.0,
                "no_repeat_ngram_size": 35,
                "ngram_window": 1024,
                "precision": self.precision,
            },
        }

    def _recognize(
        self,
        image: Path,
        *,
        task: str,
        cropping: bool,
        image_size: int,
        mode: str,
        ngram_window: int,
        embedded: EmbeddedEvidence | None = None,
        max_tokens: int | None = None,
    ) -> tuple[str, dict[str, object]]:
        self._load()
        from mlx_vlm.prompt_utils import apply_chat_template

        prompt = apply_chat_template(
            self._processor,
            self._model.config,
            task,
            num_images=1,
        )
        processors = self._decode_processors(ngram_window, [embedded] if embedded is not None else [])
        result, confidence = self._generate_with_confidence(
            _retry_factory=lambda: self._decode_processors(ngram_window, [embedded] if embedded is not None else []),
            model=self._model,
            processor=self._processor,
            image=str(image),
            prompt=prompt,
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
            temperature=0.0,
            cropping=cropping,
            image_size=image_size,
            base_size=1024,
            logits_processors=processors,
        )
        return result.text, {
            "prompt_tokens": result.prompt_tokens,
            "generation_tokens": result.generation_tokens,
            "finish_reason": result.finish_reason,
            "peak_memory_gb": result.peak_memory,
            "mode": mode,
            "confidence": confidence["summary"],
            "_confidence_spans": confidence["spans"],
            "decoding": confidence.get("decoding", self._decode_diagnostics(processors)),
            "contract": {
                "prompt": GUNDAM_PROMPT,
                "base_size": 1024,
                "image_size": image_size,
                "crop_mode": cropping,
                "temperature": 0.0,
                "no_repeat_ngram_size": 35,
                "ngram_window": ngram_window,
                **({"max_tokens": max_tokens} if max_tokens is not None else {}),
                "precision": self.precision,
            },
        }

    def _decode_processors(self, window: int, embedded: list[EmbeddedEvidence], *, single_page: bool = True):
        tokenizer = getattr(self._processor, "tokenizer", self._processor)
        processors = [SlidingWindowNoRepeatNgramProcessor(35, window)]
        processors.append(DecodeLogitsProcessor(tokenizer, embedded))
        if single_page:
            processors.append(BlockLogitsProcessor(tokenizer))
        return processors

    def _decode_diagnostics(self, processors):
        guide = next((p.guide for p in processors if isinstance(p, DecodeLogitsProcessor)), None)
        diagnostics = guide.diagnostics() if guide else {"policy": POLICY_VERSION, "unavailable": True}
        prefix = next((p for p in processors if isinstance(p, InitialGroundingProcessor)), None)
        if prefix:
            diagnostics.update(initial_grounding=GROUNDING_START_VERSION,
                               forced_prefix_tokens=len(prefix.prefix),
                               confidence_basis="post_constraint_decoder_distribution")
        block = next((p for p in processors if isinstance(p, BlockLogitsProcessor)), None)
        if block:
            diagnostics["blocks"] = block.diagnostics()
        return diagnostics

    def _generate_with_confidence(self, *, _retry_factory=None, _startup_attempted=False, **kwargs):
        """Bounded block retry. Raw alternatives remain in observation metadata."""
        from .quality import math_syntax_errors

        processors = kwargs.get("logits_processors", [])
        tracker = next((p for p in processors if isinstance(p, BlockLogitsProcessor)), None)
        result, confidence = self._stream_with_confidence(**kwargs)
        if tracker is None or _retry_factory is None:
            return result, confidence
        # Do not perturb a usable grounded body merely to remove its prefix.
        # On a genuine ungrounded failure, try one structurally steered decode.
        regions = grounded_blocks(result.text)
        body_present = any(r.kind not in {"page_number", "header", "footer"} for r in regions)
        stray_prefix = result.text.split("<|det|>", 1)[0].strip()
        if (not _startup_attempted and not body_present
                and not any(isinstance(p, InitialGroundingProcessor) for p in processors)
                and (result.finish_reason != "stop" or structural_loop(result.text)
                     or (not regions and len(result.text.strip()) < 128)
                     or (bool(regions) and len(stray_prefix) >= 128))):
            return self._recover_startup(result, confidence, _retry_factory, kwargs)
        issues = duplicate_blocks(tracker.text)
        diagnostics = self._decode_diagnostics(processors)
        confidence["decoding"] = diagnostics
        if not issues:
            return result, confidence
        fork = tracker.fork(issues[0])
        if fork is None or fork >= int(kwargs.get("max_tokens", self.max_tokens)) - 32:
            diagnostics["block_retry"] = {"skipped": "no_safe_fork_within_budget"}
            return result, confidence

        original_regions = grounded_blocks(tracker.text)
        duplicate_indices = {issue["block"] for issue in issues}
        reference = [r for i, r in enumerate(original_regions) if i not in duplicate_indices and area(r.box) > 0]

        def measure(candidate, state):
            regions = grounded_blocks(state.text)
            coverage = min((max((region_coverage(r.box, c.box) for c in regions
                                 if c.kind == r.kind), default=0.0) for r in reference), default=0.0)
            values = [v for v in state.model_scores[fork + 1:] if math.isfinite(v)]
            return {"duplicates": len(duplicate_blocks(state.text)),
                    "math_errors": len(math_syntax_errors(candidate.text)),
                    "region_coverage": coverage,
                    "mean_logprob": sum(values) / len(values) if values else None}

        original = measure(result, tracker)
        attempts = [{"raw": result.text, "finish_reason": result.finish_reason,
                     "generation_tokens": result.generation_tokens, "scores": original}]
        prefix = tracker.ids[:fork]
        banned = {tracker.ids[fork]}
        best_result, best_confidence, best_scores, selected = result, confidence, original, 0
        for attempt in range(1, 3):
            fresh = _retry_factory()
            branch = next(p for p in fresh if isinstance(p, BlockLogitsProcessor))
            fresh.append(ReplayBranchProcessor(prefix, banned))
            try:
                candidate, candidate_confidence = self._stream_with_confidence(**{**kwargs, "logits_processors": fresh})
            except ValueError as error:
                attempts.append({"error": str(error), "raw": branch.text,
                                 "generation_tokens": len(branch.ids), "eligible": False})
                break  # preserve the original instead of losing a costly page
            scores = measure(candidate, branch)
            attempts.append({"raw": candidate.text, "finish_reason": candidate.finish_reason,
                             "generation_tokens": candidate.generation_tokens, "scores": scores,
                             "banned_tokens": sorted(banned)})
            if len(branch.ids) > fork:
                banned.add(branch.ids[fork])
            eligible = (candidate.finish_reason == "stop" and branch.ids[:fork] == prefix
                        and scores["duplicates"] < original["duplicates"]
                        and scores["region_coverage"] >= .9
                        and scores["math_errors"] <= original["math_errors"]
                        and scores["mean_logprob"] is not None and original["mean_logprob"] is not None
                        and scores["mean_logprob"] >= original["mean_logprob"] - 1.0)
            attempts[-1]["eligible"] = eligible
            best_likelihood = best_scores["mean_logprob"] if best_scores["mean_logprob"] is not None else -float("inf")
            if eligible and (scores["duplicates"], -scores["mean_logprob"]) < (best_scores["duplicates"], -best_likelihood):
                best_result, best_confidence, best_scores, selected = candidate, candidate_confidence, scores, attempt
                best_confidence["decoding"] = self._decode_diagnostics(fresh)
            if eligible and scores["duplicates"] == 0:
                break
        best_confidence["decoding"]["block_retry"] = {
            "method": "fresh_state_exact_prefix_replay", "fork_token": fork,
            "selected_attempt": selected, "attempts": attempts,
            "total_generation_tokens": sum(a["generation_tokens"] for a in attempts),
            "confidence_omitted": bool(selected),
        }
        if selected:
            best_confidence["summary"], best_confidence["spans"] = None, []
        return best_result, best_confidence

    def _recover_startup(self, original, confidence, factory, kwargs):
        """One fresh initial-marker rescue; keep usable existing bodies intact."""
        def grounded_factory():
            fresh = factory()
            tracker = next(p for p in fresh if isinstance(p, BlockLogitsProcessor))
            fresh.append(InitialGroundingProcessor(tracker.tokenizer))
            return fresh

        first = {"raw": original.text, "finish_reason": original.finish_reason,
                 "generation_tokens": original.generation_tokens,
                 "decoding": self._decode_diagnostics(kwargs["logits_processors"])}
        attempts = [first]
        selected = 0
        result, selected_confidence = original, confidence
        fresh = []
        try:
            fresh = grounded_factory()
            candidate, candidate_confidence = self._generate_with_confidence(
                _retry_factory=grounded_factory, _startup_attempted=True,
                **{**kwargs, "logits_processors": fresh})
            eligible = (candidate.finish_reason == "stop" and candidate.text.startswith("<|det|>")
                        and bool(grounded_blocks(candidate.text)) and not structural_loop(candidate.text)
                        and not duplicate_blocks(candidate.text))
            retry = candidate_confidence.get("decoding", {}).get("block_retry", {})
            attempts.append({"raw": candidate.text, "finish_reason": candidate.finish_reason,
                             "generation_tokens": retry.get("total_generation_tokens", candidate.generation_tokens),
                             "decoding": candidate_confidence.get("decoding", {}), "eligible": eligible})
            if eligible:
                result, selected_confidence, selected = candidate, candidate_confidence, 1
        except ValueError as error:
            tracker = next((p for p in fresh if isinstance(p, BlockLogitsProcessor)), None)
            attempts.append({"raw": tracker.text if tracker else "", "generation_tokens": len(tracker.ids) if tracker else 0,
                             "error": str(error), "eligible": False})
        diagnostics = dict(selected_confidence.get("decoding", self._decode_diagnostics(kwargs["logits_processors"])))
        diagnostics["startup_retry"] = {"policy": "ungrounded-recovery-v1", "selected_attempt": selected,
                                        "attempts": attempts,
                                        "total_generation_tokens": sum(a["generation_tokens"] for a in attempts)}
        selected_confidence["decoding"] = diagnostics
        return result, selected_confidence

    def _stream_with_confidence(self, **kwargs):
        """Stream generation so selected-token probabilities are not discarded."""
        try:
            from mlx_vlm import stream_generate
        except ImportError:
            # Compatibility for fixture shims and older mlx-vlm builds.
            from mlx_vlm import generate

            return generate(**kwargs), {"summary": None, "spans": []}

        text = ""
        all_logprobs: list[float] = []
        generated: list[tuple[int, float | None]] = []
        last_generation_tokens = 0
        last_response = None
        tokenizer = self._processor.tokenizer if hasattr(self._processor, "tokenizer") else self._processor
        special_ids = set(getattr(tokenizer, "all_special_ids", []) or [])
        stream = stream_generate(**kwargs)
        loop_since = None
        processors = kwargs.get("logits_processors", [])
        startup_guard = (any(isinstance(p, BlockLogitsProcessor) for p in processors)
                         and not any(isinstance(p, InitialGroundingProcessor) for p in processors))
        try:
            for response in stream:
                generation_tokens = int(response.generation_tokens or 0)
                if generation_tokens > last_generation_tokens:
                    selected = _selected_logprob(response.token, response.logprobs)
                    token = int(response.token)
                    generated.append((token, selected))
                    if selected is not None and token not in special_ids:
                        all_logprobs.append(selected)
                    last_generation_tokens = generation_tokens
                segment = response.text or ""
                if segment:
                    text += segment
                last_response = response
                # Bound ungrounded startup independently of the full-page budget.
                # Beyond this tested limit a failed decoder can fabricate a late
                # grounded table and evade rescue. Ordinary grounded pages keep
                # their full token budget; the raw failed attempt is retained.
                if startup_guard and generation_tokens >= 4096 and generation_tokens % 32 == 0:
                    regions = grounded_blocks(text)
                    if not any(r.kind not in {"page_number", "header", "footer"} for r in regions):
                        last_response.finish_reason = "ungrounded_guard"
                        break
                # A short garbage prefix may recover. Stop only sustained loops
                # after soft steering has had a bounded opportunity to escape.
                if generation_tokens >= 512 and generation_tokens % 32 == 0:
                    source_progress = any(isinstance(p, DecodeLogitsProcessor) and p.guide.recent_source_progress
                                          for p in kwargs.get("logits_processors", []))
                    looping = not source_progress and loop_pattern(structural_atoms(text[-8192:])) is not None
                    loop_since = (loop_since if loop_since is not None else generation_tokens) if looping else None
                    if loop_since is not None and generation_tokens - loop_since >= 256:
                        last_response.finish_reason = "repetition_guard"
                        break
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        if last_response is None:
            from types import SimpleNamespace

            last_response = SimpleNamespace(
                prompt_tokens=0,
                generation_tokens=0,
                finish_reason="length",
                peak_memory=0.0,
            )
        last_response.text = text
        return last_response, {
            "summary": confidence_summary(all_logprobs),
            "spans": _align_token_confidence(text, generated, tokenizer, special_ids),
        }


class SlidingWindowNoRepeatNgramProcessor:
    """MLX port of Unlimited-OCR's opt-in repetition guard."""

    def __init__(self, ngram_size: int, window: int, whitelist_token_ids: list[int] | None = None):
        self.ngram_size = ngram_size
        self.window = window
        self.whitelist = set(whitelist_token_ids or [])

    def __call__(self, input_ids, logits):
        import mlx.core as mx

        single = input_ids.ndim == 1
        ids = mx.expand_dims(input_ids, 0) if single else input_ids
        scores = mx.expand_dims(logits, 0) if logits.ndim == 1 else logits
        rows = []
        for batch_index in range(ids.shape[0]):
            sequence = ids[batch_index].tolist()
            banned: set[int] = set()
            if len(sequence) >= self.ngram_size:
                search_start = max(0, len(sequence) - self.window)
                search_end = len(sequence) - self.ngram_size + 1
                prefix = tuple(sequence[-(self.ngram_size - 1) :]) if self.ngram_size > 1 else ()
                for index in range(search_start, max(search_start, search_end)):
                    ngram = sequence[index : index + self.ngram_size]
                    if self.ngram_size == 1 or tuple(ngram[:-1]) == prefix:
                        banned.add(ngram[-1])
            banned.difference_update(self.whitelist)
            row = scores[batch_index]
            if banned:
                indices = mx.array(sorted(banned), dtype=mx.int32)
                row = row.at[indices].add(mx.full((len(banned),), float("-inf"), dtype=row.dtype))
            rows.append(row)
        result = mx.stack(rows)
        return result[0] if logits.ndim == 1 else result


def _cast_arrays(value, dtype):
    if isinstance(value, list):
        return [_cast_arrays(item, dtype) for item in value]
    if isinstance(value, tuple):
        return tuple(_cast_arrays(item, dtype) for item in value)
    return value.astype(dtype) if hasattr(value, "astype") and hasattr(value, "dtype") else value


def _selected_logprob(token, logprobs) -> float | None:
    if token is None or logprobs is None:
        return None
    try:
        value = logprobs[int(token)]
        return float(value.item() if hasattr(value, "item") else value)
    except (IndexError, TypeError, ValueError):
        return None


def _align_token_confidence(text, generated, tokenizer, special_ids) -> list[dict[str, object]]:
    spans: list[dict[str, object]] = []
    cursor = 0
    for token, logprob in generated:
        piece = tokenizer.decode(
            [token],
            skip_special_tokens=False,
            clean_up_tokenization_spaces=False,
        )
        if not piece:
            continue
        start = text.find(piece, cursor)
        if start < 0:
            stripped = piece.lstrip()
            start = text.find(stripped, cursor) if stripped else -1
            piece = stripped if start >= 0 else piece
        if start < 0:
            continue
        end = start + len(piece)
        cursor = end
        if logprob is not None and token not in special_ids:
            spans.append({"start": start, "end": end, "logprobs": [logprob]})
    return spans


def confidence_summary(logprobs: list[float]) -> dict[str, float | int] | None:
    if not logprobs:
        return None
    probabilities = sorted(math.exp(max(-100.0, min(0.0, value))) for value in logprobs)
    fifth = probabilities[max(0, math.ceil(len(probabilities) * 0.05) - 1)]
    return {
        "token_count": len(probabilities),
        "geometric_mean_probability": round(math.exp(sum(logprobs) / len(logprobs)), 6),
        "p05_probability": round(fifth, 6),
        "minimum_probability": round(probabilities[0], 6),
        "below_half_fraction": round(sum(value < 0.5 for value in probabilities) / len(probabilities), 6),
    }


def split_multi_page_output(raw: str, expected_pages: int) -> list[str]:
    pages = [part.strip() for part in re.split(r"\s*<PAGE>\s*", raw) if part.strip()]
    if not pages:
        raise RuntimeError(
            f"multi-page OCR returned {len(pages)} page segment(s) for {expected_pages} input page(s)"
        )
    return pages
