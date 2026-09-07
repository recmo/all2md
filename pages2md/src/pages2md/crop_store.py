"""Document-scoped crop evidence store. Existing observations are never overwritten."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from PIL import Image

from .model import RecoveryAttempt
from .native import parse_native_observation
from .regions import valid_box
from .util import atomic_json


def crop_observation(value):
    """Decode one validated crop response and restore page coordinates."""
    if (not isinstance(value, dict) or not valid_box(value.get("bbox"))
            or not isinstance(value.get("raw"), str)
            or not isinstance(value.get("page"), int)
            or not isinstance(value.get("generation"), dict)):
        raise ValueError("invalid crop observation")
    observation = parse_native_observation(value["raw"], mode="region_detail",
                                          source_pages=[value["page"]],
                                          generation=value["generation"])
    x0, y0, x1, y1 = value["bbox"]
    observation.id += "-" + hashlib.sha256(json.dumps(value["bbox"]).encode()).hexdigest()[:8]
    for block in observation.blocks:
        if valid_box(block.bbox):
            a, b, c, d = block.bbox
            block.bbox = (x0+a*(x1-x0)/1000, y0+b*(y1-y0)/1000,
                          x0+c*(x1-x0)/1000, y0+d*(y1-y0)/1000)
        else:
            block.bbox = None
    return observation


class CropStore:
    """Read the legacy flat cache once per document, indexed by source hash."""

    def __init__(self, bundle: Path):
        self.bundle = Path(bundle)
        self.directory = self.bundle / "region-observations"
        self.directory.mkdir(exist_ok=True)
        self._by_source = defaultdict(list)
        self._invalid = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                value = json.loads(path.read_text())
                if not isinstance(value, dict) or not isinstance(value.get("source_hash"), str):
                    raise ValueError("missing source hash")
            except (ValueError, OSError):
                self._invalid.append(self._failure(path, "invalid_cache", "invalid_cache"))
                continue
            self._by_source[value["source_hash"]].append((path, value))

    def _failure(self, path, reason, error):
        return RecoveryAttempt(reason, cache=str(path.relative_to(self.bundle)), error=error)

    def _decode(self, path, value):
        if "raw" not in value:
            return None, self._failure(path, "recognition_failed", value.get("error", "invalid_cache"))
        # Only decoding is caught here. Evaluation errors must propagate.
        try:
            observation = crop_observation(value)
        except (ValueError, TypeError, KeyError):
            return None, self._failure(path, "invalid_cache", "invalid_cache")
        return observation, None

    def replay(self, source_hash):
        # Unattributable corrupt records are reported once, not once per page.
        invalid, self._invalid = self._invalid, []
        for failure in invalid:
            yield None, failure.cache, failure
        for path, value in self._by_source[source_hash]:
            observation, failure = self._decode(path, value)
            yield observation, str(path.relative_to(self.bundle)), failure

    def request(self, source_page, source_hash, box, scale, backend):
        """Return (observation, relative path, failure), or None for an existing request."""
        identity = dict(backend.identity)
        key = hashlib.sha256(json.dumps(
            [1, source_hash, identity, box, scale], sort_keys=True).encode()).hexdigest()
        path = self.directory / f"{key}.json"
        if path.exists():
            return None
        crop_path = path.with_suffix(".png")
        with Image.open(source_page.image_path) as image:
            w, h = image.size
            a, b, c, d = box
            crop = image.crop((int(a*w/1000), int(b*h/1000), int(c*w/1000), int(d*h/1000)))
            if scale != 1:
                crop = crop.resize((crop.width * scale, crop.height * scale))
            crop.save(crop_path)
        try:
            raw, generation = backend.recognize_detail(crop_path)
            value = {"raw": raw, "generation": dict(generation), "bbox": box,
                     "page": source_page.number, "source_hash": source_hash}
        except Exception as error:
            value = {"error": type(error).__name__, "page": source_page.number,
                     "source_hash": source_hash}
        atomic_json(path, value)
        self._by_source[source_hash].append((path, value))
        observation, failure = self._decode(path, value)
        return observation, str(path.relative_to(self.bundle)), failure
