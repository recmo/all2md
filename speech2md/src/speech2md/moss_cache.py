from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .model import AudioSource
from .moss import (
    MAX_GENERATION_TOKENS,
    MAX_RECOVERY_ATTEMPTS,
    MIN_RECOVERY_PROGRESS_SECONDS,
    MOSS_MODEL,
    MOSS_REVISION,
    RECOVERY_OVERLAP_SECONDS,
    RECOVERY_TOKEN_THRESHOLD,
    SILENCE_MIN_SECONDS,
    SILENCE_NOISE_DB,
    SILENCE_SEARCH_SECONDS,
    TARGET_PART_SECONDS,
    WINDOW_OVERLAP_SECONDS,
)


CACHE_SCHEMA_VERSION = 1
CACHE_COMPATIBILITY_IGNORED_KEYS = {"speech2md_version"}


class MossCacheMiss(RuntimeError):
    pass


def cache_path(markdown_path: Path) -> Path:
    return markdown_path.with_suffix(".moss.npz")


def cache_metadata(
    *, prompt: str, hotwords: tuple[str, ...], sources: list[AudioSource]
) -> dict[str, Any]:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "model": MOSS_MODEL,
        "model_revision": MOSS_REVISION,
        "prompt": prompt,
        "max_generation_tokens": MAX_GENERATION_TOKENS,
        "windowing": {
            "target_part_seconds": TARGET_PART_SECONDS,
            "overlap_seconds": WINDOW_OVERLAP_SECONDS,
            "silence_search_seconds": SILENCE_SEARCH_SECONDS,
            "silence_noise_db": SILENCE_NOISE_DB,
            "silence_min_seconds": SILENCE_MIN_SECONDS,
        },
        "recovery": {
            "token_threshold": RECOVERY_TOKEN_THRESHOLD,
            "overlap_seconds": RECOVERY_OVERLAP_SECONDS,
            "minimum_progress_seconds": MIN_RECOVERY_PROGRESS_SECONDS,
            "maximum_attempts": MAX_RECOVERY_ATTEMPTS,
        },
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
    if not isinstance(metadata, dict) or not isinstance(tracks, dict):
        return {}
    comparable_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in CACHE_COMPATIBILITY_IGNORED_KEYS
    }
    comparable_expected = {
        key: value
        for key, value in expected_metadata.items()
        if key not in CACHE_COMPATIBILITY_IGNORED_KEYS
    }
    if comparable_metadata != comparable_expected:
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
