from __future__ import annotations

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


class MlxUnlimitedOcr:
    identity = {"engine": "mlx-vlm", "model": MODEL_ID, "revision": MODEL_REVISION}

    def __init__(self, max_tokens: int = 32768):
        self.max_tokens = max_tokens
        self._model = None
        self._processor = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from mlx_vlm import load
        except ImportError as error:
            raise RuntimeError("MLX OCR dependencies are missing; install ebook2md[ocr]") from error
        self._model, self._processor = load(MODEL_ID, revision=MODEL_REVISION)

    def recognize(self, image: Path) -> tuple[str, dict[str, object]]:
        return self._recognize(
            image,
            task=GUNDAM_PROMPT.removeprefix("<image>"),
            cropping=True,
            image_size=640,
            mode="gundam",
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
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        prompt = apply_chat_template(
            self._processor,
            self._model.config,
            "Multi page parsing.",
            num_images=len(images),
        )
        result = generate(
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
            "contract": {
                "prompt": MULTI_PAGE_PROMPT,
                "base_size": 1024,
                "image_size": 1024,
                "crop_mode": False,
                "temperature": 0.0,
                "no_repeat_ngram_size": 35,
                "ngram_window": 1024,
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
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        prompt = apply_chat_template(
            self._processor,
            self._model.config,
            task,
            num_images=1,
        )
        result = generate(
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
            "contract": {
                "prompt": GUNDAM_PROMPT,
                "base_size": 1024,
                "image_size": image_size,
                "crop_mode": cropping,
                "temperature": 0.0,
                "no_repeat_ngram_size": 35,
                "ngram_window": ngram_window,
            },
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


def split_multi_page_output(raw: str, expected_pages: int) -> list[str]:
    pages = [part.strip() for part in re.split(r"\s*<PAGE>\s*", raw) if part.strip()]
    if not pages or len(pages) > expected_pages:
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
