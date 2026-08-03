from __future__ import annotations

from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
import math
import subprocess
import tempfile
from typing import Any

from .model import Segment

MOSS_MODEL = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
MOSS_REVISION = "e8681d68e7042738ffca8ac8212bc8fcb1131ab8"
DEFAULT_WINDOW_SECONDS = 300.0
DEFAULT_OVERLAP_SECONDS = 5.0
MAX_GENERATION_TOKENS = 65_536


def plan_windows(duration: float, *, window_seconds: float, overlap_seconds: float) -> list[tuple[float, float]]:
    if window_seconds <= 0:
        raise ValueError("window seconds must be positive")
    if overlap_seconds < 0 or overlap_seconds >= window_seconds:
        raise ValueError("overlap seconds must be non-negative and shorter than the window")
    windows = []
    start = 0.0
    while start < duration:
        end = min(duration, start + window_seconds)
        windows.append((start, end))
        if end >= duration:
            break
        start = end - overlap_seconds
    return windows


def parse_segments(items: list[dict[str, Any]], *, window: int, offset: float, role: str) -> list[Segment]:
    segments = []
    for item in items or []:
        speaker_id = str(item.get("speaker_id", "S01"))
        digits = "".join(character for character in speaker_id if character.isdigit())
        local_speaker = f"W{window:02d}:S{int(digits or '1'):02d}"
        text = str(item.get("text", "")).removeprefix(f"[{speaker_id}]").strip()
        if text:
            segments.append(Segment(
                start=offset + float(item["start"]),
                end=offset + float(item["end"]),
                text=text,
                speaker="Remco" if role == "microphone" else local_speaker,
                source_role=role,
            ))
    return segments


def transcribe_track(
    path: Path,
    *,
    role: str,
    duration: float,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> tuple[list[Segment], dict[str, Any]]:
    try:
        from mlx_audio.stt import load
    except ImportError as error:
        raise RuntimeError("MOSS runtime is unavailable; run audio2md through its locked environment") from error

    windows = plan_windows(duration, window_seconds=window_seconds, overlap_seconds=overlap_seconds)
    engine = load(MOSS_MODEL, revision=MOSS_REVISION)
    raw_windows: list[dict[str, Any]] = []
    by_window: list[list[Segment]] = []
    with tempfile.TemporaryDirectory(prefix="audio2md-moss-") as temporary:
        directory = Path(temporary)
        for index, (start, end) in enumerate(windows, 1):
            chunk = directory / f"window-{index:03d}.wav"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-ss", str(start),
                    "-t", str(end - start), "-i", str(path), "-map", "0:a:0",
                    "-ac", "1", "-ar", "16000", "-y", str(chunk),
                ],
                check=True,
            )
            result = engine.generate(str(chunk), max_tokens=MAX_GENERATION_TOKENS)
            segments = parse_segments(
                getattr(result, "segments", []) or [],
                window=index,
                offset=start,
                role=role,
            )
            by_window.append(segments)
            raw_windows.append({
                "index": index,
                "source_start": start,
                "source_end": end,
                "generation_tokens": getattr(result, "generation_tokens", None),
                "segments": [asdict(segment) for segment in segments],
            })

    mapping, joins = reconcile_speakers(by_window, windows, role=role)
    for segments in by_window:
        for segment in segments:
            segment.speaker = mapping.get(segment.speaker, segment.speaker)
    selected = trim_overlaps(by_window, windows)
    selected = deduplicate_boundaries(selected)
    return selected, {
        "role": role,
        "audio": str(path),
        "windows": raw_windows,
        "speaker_mapping": mapping,
        "overlap_joins": joins,
    }


def reconcile_speakers(
    by_window: list[list[Segment]],
    windows: list[tuple[float, float]],
    *,
    role: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if role == "microphone":
        return {
            segment.speaker: "Remco"
            for segments in by_window
            for segment in segments
        }, []

    mapping: dict[str, str] = {}
    joins: list[dict[str, Any]] = []
    next_speaker = 1
    for window_index, segments in enumerate(by_window):
        local_speakers = sorted({segment.speaker for segment in segments})
        candidates: list[tuple[float, str, str]] = []
        if window_index:
            overlap_start = windows[window_index][0]
            overlap_end = windows[window_index - 1][1]
            previous = [
                segment for segment in by_window[window_index - 1]
                if segment.end > overlap_start and segment.start < overlap_end
            ]
            current = [
                segment for segment in segments
                if segment.end > overlap_start and segment.start < overlap_end
            ]
            for local in local_speakers:
                for global_speaker in sorted({mapping[item.speaker] for item in previous if item.speaker in mapping}):
                    score = overlap_match_score(
                        [item for item in current if item.speaker == local],
                        [item for item in previous if mapping.get(item.speaker) == global_speaker],
                    )
                    if score >= 0.5:
                        candidates.append((score, local, global_speaker))

        assigned_local: set[str] = set()
        assigned_global: set[str] = set()
        for score, local, global_speaker in sorted(candidates, reverse=True):
            if local in assigned_local or global_speaker in assigned_global:
                continue
            mapping[local] = global_speaker
            assigned_local.add(local)
            assigned_global.add(global_speaker)
            joins.append({
                "window": window_index + 1,
                "local_speaker": local,
                "speaker": global_speaker,
                "overlap_score": score,
            })
        for local in local_speakers:
            if local not in mapping:
                mapping[local] = f"Speaker {next_speaker}"
                next_speaker += 1
    return mapping, joins


def overlap_match_score(left: list[Segment], right: list[Segment]) -> float:
    score = 0.0
    for first in left:
        for second in right:
            overlap = max(0.0, min(first.end, second.end) - max(first.start, second.start))
            if not overlap:
                continue
            similarity = SequenceMatcher(None, first.text.lower(), second.text.lower()).ratio()
            score += overlap * similarity
    return score


def trim_overlaps(by_window: list[list[Segment]], windows: list[tuple[float, float]]) -> list[Segment]:
    output = []
    for index, segments in enumerate(by_window):
        left = 0.0 if index == 0 else (windows[index - 1][1] + windows[index][0]) / 2
        right = math.inf if index == len(windows) - 1 else (windows[index][1] + windows[index + 1][0]) / 2
        output.extend(segment for segment in segments if left <= (segment.start + segment.end) / 2 < right)
    return sorted(output, key=lambda segment: (segment.start, segment.end))


def deduplicate_boundaries(segments: list[Segment]) -> list[Segment]:
    dropped: set[int] = set()
    for left_index, left in enumerate(segments):
        if left_index in dropped:
            continue
        for right_index in range(left_index + 1, len(segments)):
            right = segments[right_index]
            if right.start >= left.end:
                break
            if right_index in dropped or left.speaker != right.speaker:
                continue
            overlap = min(left.end, right.end) - max(left.start, right.start)
            similarity = SequenceMatcher(None, left.text.lower(), right.text.lower()).ratio()
            if overlap >= 0.5 and similarity >= 0.65:
                if len(left.text) >= len(right.text):
                    dropped.add(right_index)
                else:
                    dropped.add(left_index)
                    break
    return [segment for index, segment in enumerate(segments) if index not in dropped]
