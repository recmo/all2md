from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from .constants import MODEL_ID, MODEL_REVISION
from .model import Block

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
        self._load()
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        prompt = apply_chat_template(
            self._processor,
            self._model.config,
            "document parsing.",
            num_images=1,
        )
        result = generate(
            model=self._model,
            processor=self._processor,
            image=str(image),
            prompt=prompt,
            max_tokens=self.max_tokens,
            temperature=0.0,
            cropping=True,
            image_size=640,
            base_size=1024,
        )
        return result.text, {
            "prompt_tokens": result.prompt_tokens,
            "generation_tokens": result.generation_tokens,
            "finish_reason": result.finish_reason,
            "peak_memory_gb": result.peak_memory,
        }


class SidecarOcr:
    """Test/debug backend reading `<page-image>.ocr.txt` files."""

    identity = {"engine": "sidecar", "model": "fixture", "revision": "1"}

    def recognize(self, image: Path) -> tuple[str, dict[str, object]]:
        sidecar = image.with_suffix(image.suffix + ".ocr.txt")
        if not sidecar.exists():
            raise RuntimeError(f"missing OCR sidecar: {sidecar}")
        return sidecar.read_text(encoding="utf-8"), {}

