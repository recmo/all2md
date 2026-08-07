from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .model import AudioSource, ResolvedInput

MEDIA_SUFFIXES = {".aac", ".caf", ".flac", ".m4a", ".mka", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def probe(
    path: Path,
    *,
    expected_sha256: str | None = None,
    role: str = "mixed",
    stream_index: int = 0,
) -> AudioSource:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = sha256(path)
    if expected_sha256 and actual != expected_sha256:
        raise ValueError(f"checksum mismatch for {path.name}: expected {expected_sha256}, got {actual}")
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", f"a:{stream_index}",
            "-show_entries", "stream=codec_name,duration:format=duration,format_name",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(process.stdout)
    streams = value.get("streams", [])
    if not streams:
        raise ValueError(f"no audio stream in {path}")
    duration = streams[0].get("duration") or value.get("format", {}).get("duration") or 0
    return AudioSource(
        path=str(path.resolve()),
        role=role,
        sha256=actual,
        duration_seconds=float(duration),
        format=streams[0].get("codec_name") or path.suffix.lstrip(".").lower(),
        stream_index=stream_index,
    )


def resolve_input(requested: Path) -> ResolvedInput:
    requested = requested.expanduser().resolve()
    if requested.suffix.lower() != ".json":
        if requested.suffix.lower() not in MEDIA_SUFFIXES:
            raise ValueError(f"unsupported input: {requested}")
        stem = requested.with_suffix("")
        return ResolvedInput(
            requested=requested,
            markdown_path=stem.with_suffix(".md"),
            title=None,
            started_at=None,
            ended_at=None,
            calendar_event=None,
            sources=((requested, "mixed", None, 0),),
        )

    value = json.loads(requested.read_text())
    schema_version = value.get("schemaVersion")
    if schema_version not in {1, 2}:
        raise ValueError("unsupported meeting capture schema version")
    required = {"meetingID", "audio", "status", "startedAt", "endedAt"}
    missing = required - value.keys()
    if missing:
        raise ValueError("capture manifest missing: " + ", ".join(sorted(missing)))
    sources: list[tuple[Path, str, str | None, int]] = []
    if schema_version == 1:
        for track in value["audio"]:
            if not {"file", "role", "sha256"} <= track.keys():
                raise ValueError("capture audio track is incomplete")
            sources.append((requested.parent / track["file"], track["role"], track["sha256"], 0))
    else:
        container = value.get("container")
        if not isinstance(container, dict) or not {"file", "sha256"} <= container.keys():
            raise ValueError("capture container is incomplete")
        container_path = requested.parent / container["file"]
        for track in value["audio"]:
            if not {"streamIndex", "role"} <= track.keys():
                raise ValueError("capture audio track is incomplete")
            sources.append((container_path, track["role"], container["sha256"], int(track["streamIndex"])))
    base = requested.name.removesuffix("-capture.json")
    return ResolvedInput(
        requested=requested,
        markdown_path=requested.parent / f"{base}.md",
        title=value.get("title"),
        started_at=value.get("startedAt"),
        ended_at=value.get("endedAt"),
        calendar_event=(
            value.get("calendarEventID")
            if str(value.get("calendarEventID", "")).startswith(("https://", "http://"))
            else None
        ),
        sources=tuple(sources),
    )
