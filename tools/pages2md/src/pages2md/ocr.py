from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Protocol

from .constants import MODEL_ID, MODEL_REVISION
from .model import Block

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

    def recognize(self, image: Path) -> tuple[str, dict[str, object]]: ...

    def recognize_pages(self, images: list[Path]) -> tuple[str, dict[str, object]]: ...

    def recognize_detail(self, image: Path) -> tuple[str, dict[str, object]]: ...


class MlxUnlimitedOcr:
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

    def recognize(self, image: Path) -> tuple[str, dict[str, object]]:
        return self._recognize(
            image,
            task=GUNDAM_PROMPT.removeprefix("<image>"),
            cropping=True,
            image_size=640,
            mode="gundam",
            ngram_window=128,
        )

    def recognize_detail(self, image: Path) -> tuple[str, dict[str, object]]:
        return self._recognize(
            image,
            task=GUNDAM_PROMPT.removeprefix("<image>"),
            cropping=True,
            image_size=1024,
            mode="gundam_detail",
            ngram_window=128,
        )

    def recognize_pages(self, images: list[Path]) -> tuple[str, dict[str, object]]:
        """Port Unlimited-OCR's `infer_multi` contract to MLX.

        The reference implementation expands every page at one image-token position.
        mlx-vlm requires one marker per image, but with no text between the markers it
        produces the same contiguous image-token sequence.
        """
        if not images:
            return []
        self._load()
        from mlx_vlm.prompt_utils import apply_chat_template

        prompt = apply_chat_template(
            self._processor,
            self._model.config,
            "Multi page parsing.",
            num_images=len(images),
        )
        result, confidence = self._generate_with_confidence(
            model=self._model,
            processor=self._processor,
            image=[str(image) for image in images],
            prompt=prompt,
            max_tokens=self.max_tokens,
            temperature=0.0,
            cropping=False,
            image_size=1024,
            base_size=1024,
            logits_processors=[SlidingWindowNoRepeatNgramProcessor(35, 1024)],
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
    ) -> tuple[str, dict[str, object]]:
        self._load()
        from mlx_vlm.prompt_utils import apply_chat_template

        prompt = apply_chat_template(
            self._processor,
            self._model.config,
            task,
            num_images=1,
        )
        result, confidence = self._generate_with_confidence(
            model=self._model,
            processor=self._processor,
            image=str(image),
            prompt=prompt,
            max_tokens=self.max_tokens,
            temperature=0.0,
            cropping=cropping,
            image_size=image_size,
            base_size=1024,
            logits_processors=[SlidingWindowNoRepeatNgramProcessor(35, ngram_window)],
        )
        return result.text, {
            "prompt_tokens": result.prompt_tokens,
            "generation_tokens": result.generation_tokens,
            "finish_reason": result.finish_reason,
            "peak_memory_gb": result.peak_memory,
            "mode": mode,
            "confidence": confidence["summary"],
            "_confidence_spans": confidence["spans"],
            "contract": {
                "prompt": GUNDAM_PROMPT,
                "base_size": 1024,
                "image_size": image_size,
                "crop_mode": cropping,
                "temperature": 0.0,
                "no_repeat_ngram_size": 35,
                "ngram_window": ngram_window,
                "precision": self.precision,
            },
        }

    def _generate_with_confidence(self, **kwargs):
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
        for response in stream_generate(**kwargs):
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


class SidecarOcr:
    """Test/debug backend reading `<page-image>.ocr.txt` files."""

    identity = {"engine": "sidecar", "model": "fixture", "revision": "1"}

    def recognize(self, image: Path) -> tuple[str, dict[str, object]]:
        sidecar = image.with_suffix(image.suffix + ".ocr.txt")
        if not sidecar.exists():
            raise RuntimeError(f"missing OCR sidecar: {sidecar}")
        return sidecar.read_text(encoding="utf-8"), {}

    def recognize_pages(self, images: list[Path]) -> tuple[str, dict[str, object]]:
        values = [self.recognize(image)[0] for image in images]
        return "\n<PAGE>\n".join(values), {"mode": "multi_base", "group_size": len(images)}
