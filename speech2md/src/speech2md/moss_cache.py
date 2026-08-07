from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np

from .model import AudioSource
from .moss import (
    FRESH_SPEAKER_MIN_PROBABILITY,
    FRESH_SPEAKER_MIN_TOP_RATIO,
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


CACHE_SCHEMA_VERSION = 2
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
        "speaker_novelty": {
            "minimum_probability": FRESH_SPEAKER_MIN_PROBABILITY,
            "minimum_top_ratio": FRESH_SPEAKER_MIN_TOP_RATIO,
        },
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
            (
                {"role": source.role, "sha256": source.sha256, "stream_index": source.stream_index}
                for source in sources
            ),
            key=lambda item: (item["role"], item["sha256"], item["stream_index"]),
        ),
    }


def source_key(source: AudioSource) -> str:
    return f"{source.role}:{source.stream_index}:{source.sha256}"


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
    if not all(
        value.get(key) is None
        or (isinstance(value.get(key), int) and not isinstance(value.get(key), bool))
        for key in ("prompt_tokens", "generation_tokens", "total_tokens")
    ):
        return False
    base = value.get("base")
    if base is not None and (
        not isinstance(base, dict)
        or "base" in base
        or "speaker_forces" in base
        or not _valid_generation(base)
    ):
        return False
    forces = value.get("speaker_forces")
    if forces is not None and (
        not isinstance(forces, list)
        or not forces
        or any(not _valid_speaker_force(force) for force in forces)
    ):
        return False
    if (base is None) != (forces is None):
        return False
    decisions = value.get("speaker_decisions")
    if decisions is not None and (
        not isinstance(decisions, list)
        or any(not isinstance(decision, dict) for decision in decisions)
    ):
        return False
    return True


def _valid_speaker_force(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    start = value.get("start")
    return (
        isinstance(start, (int, float))
        and not isinstance(start, bool)
        and math.isfinite(start)
        and start >= 0
        and isinstance(value.get("speaker"), str)
        and re.fullmatch(r"S\d+", value["speaker"]) is not None
        and isinstance(value.get("identity"), str)
        and bool(value["identity"].strip())
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
