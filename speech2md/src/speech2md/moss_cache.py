from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .model import AudioSource
from .moss import MAX_GENERATION_TOKENS, MOSS_MODEL, MOSS_REVISION


CACHE_SCHEMA_VERSION = 1


def cache_path(markdown_path: Path) -> Path:
    return markdown_path.with_suffix(".moss.npz")


def cache_metadata(
    *, version: str, hotwords: tuple[str, ...], sources: list[AudioSource]
) -> dict[str, Any]:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "speech2md_version": version,
        "model": MOSS_MODEL,
        "model_revision": MOSS_REVISION,
        "max_generation_tokens": MAX_GENERATION_TOKENS,
        "hotwords": list(hotwords),
        "sources": sorted(
            ({"role": source.role, "sha256": source.sha256} for source in sources),
            key=lambda item: (item["role"], item["sha256"]),
        ),
    }


def source_key(source: AudioSource) -> str:
    return f"{source.role}:{source.sha256}"


def load_cache(path: Path, expected_metadata: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if not path.is_file():
        return {}
    try:
        with np.load(path, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata"].item()))
            tracks = json.loads(str(archive["tracks"].item()))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {}
    if metadata != expected_metadata or not isinstance(tracks, dict):
        return {}
    if not all(
        isinstance(key, str)
        and isinstance(value, list)
        and all(_valid_generation(item) for item in value)
        for key, value in tracks.items()
    ):
        return {}
    return tracks


def _valid_generation(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        return False
    return all(
        value.get(key) is None
        or (isinstance(value.get(key), int) and not isinstance(value.get(key), bool))
        for key in ("prompt_tokens", "generation_tokens", "total_tokens")
    )


def write_cache(
    path: Path,
    metadata: dict[str, Any],
    tracks: dict[str, list[dict[str, Any]]],
) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as handle:
        np.savez_compressed(
            handle,
            metadata=np.array(json.dumps(metadata, sort_keys=True)),
            tracks=np.array(json.dumps(tracks, ensure_ascii=False)),
        )
    temporary.chmod(0o600)
    temporary.replace(path)
