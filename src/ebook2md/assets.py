from __future__ import annotations

import json
import mimetypes
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from .util import atomic_json, sha256_bytes


@dataclass
class Asset:
    id: str
    path: str
    original_path: str | None
    sha256: str
    mime_type: str
    width: int | None
    height: int | None
    page: int
    bbox: tuple[float, float, float, float] | None
    extraction_method: str
    source_object: str | None = None
    caption: str | None = None
    alt_text: str = "Figure"


class AssetStore:
    def __init__(self, root: Path):
        self.root = root
        self.figures = root / "figures"
        self.originals = root / "originals"
        self.evidence = root / "evidence"
        for path in (self.figures, self.originals, self.evidence):
            path.mkdir(parents=True, exist_ok=True)
        manifest = root / "manifest.json"
        self.assets: list[Asset] = []
        if manifest.exists():
            for item in json.loads(manifest.read_text(encoding="utf-8")).get("assets", []):
                if item.get("bbox") is not None:
                    item["bbox"] = tuple(item["bbox"])
                self.assets.append(Asset(**item))
        self._display_by_hash: dict[str, str] = {
            asset.sha256: asset.path.removeprefix("assets/") for asset in self.assets
        }

    def add_bytes(
        self,
        data: bytes,
        *,
        extension: str,
        page: int,
        bbox: tuple[float, float, float, float] | None,
        method: str,
        source_object: str | None = None,
        caption: str | None = None,
        alt_text: str = "Figure",
        preserve_original: bool = True,
    ) -> Asset:
        digest = sha256_bytes(data)
        asset_number = len(self.assets) + 1
        asset_id = f"fig-{asset_number:04d}"
        extension = extension.lower().lstrip(".") or "bin"
        original_rel: str | None = None
        if preserve_original:
            original_rel = f"originals/{digest[:16]}.{extension}"
            original_path = self.root / original_rel
            if not original_path.exists():
                original_path.write_bytes(data)

        display_data, display_ext, width, height = self._display(data, extension)
        display_hash = sha256_bytes(display_data)
        if display_hash in self._display_by_hash:
            display_rel = self._display_by_hash[display_hash]
        else:
            display_rel = f"figures/{asset_id}.{display_ext}"
            (self.root / display_rel).write_bytes(display_data)
            self._display_by_hash[display_hash] = display_rel
        mime = mimetypes.guess_type(display_rel)[0] or "application/octet-stream"
        asset = Asset(
            id=asset_id,
            path=f"assets/{display_rel}",
            original_path=f"assets/{original_rel}" if original_rel else None,
            sha256=digest,
            mime_type=mime,
            width=width,
            height=height,
            page=page,
            bbox=bbox,
            extraction_method=method,
            source_object=source_object,
            caption=caption,
            alt_text=alt_text[:160] or "Figure",
        )
        self.assets.append(asset)
        return asset

    def add_crop(
        self,
        page_image: Path,
        normalized_bbox: tuple[float, float, float, float],
        *,
        page: int,
        caption: str | None,
        alt_text: str,
        evidence: bool = False,
    ) -> Asset:
        with Image.open(page_image) as image:
            width, height = image.size
            x0, y0, x1, y1 = normalized_bbox
            box = (
                max(0, round(x0 * width / 1000)),
                max(0, round(y0 * height / 1000)),
                min(width, round(x1 * width / 1000)),
                min(height, round(y1 * height / 1000)),
            )
            crop = image.crop(box)
            buffer = BytesIO()
            crop.save(buffer, format="PNG")
        asset = self.add_bytes(
            buffer.getvalue(),
            extension="png",
            page=page,
            bbox=normalized_bbox,
            method="rendered_bbox_crop",
            caption=caption,
            alt_text=alt_text,
            preserve_original=False,
        )
        if evidence:
            source = self.root.parent / asset.path
            evidence_rel = f"evidence/equation-page-{page:04d}-{asset.id[4:]}.png"
            (self.root / evidence_rel).write_bytes(source.read_bytes())
        return asset

    def write_manifest(self) -> None:
        atomic_json(self.root / "manifest.json", {"version": 1, "assets": [asdict(asset) for asset in self.assets]})

    def get(self, asset_id: str) -> Asset | None:
        return next((asset for asset in self.assets if asset.id == asset_id), None)

    @staticmethod
    def _display(data: bytes, extension: str) -> tuple[bytes, str, int | None, int | None]:
        if extension == "svg":
            return data, "svg", None, None
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if extension in {"png", "jpg", "jpeg", "gif", "webp"}:
                    return data, extension, width, height
                converted = image.convert("RGBA" if image.mode in {"RGBA", "LA"} else "RGB")
                buffer = BytesIO()
                converted.save(buffer, format="PNG")
                return buffer.getvalue(), "png", width, height
        except Exception:
            return data, extension, None, None
