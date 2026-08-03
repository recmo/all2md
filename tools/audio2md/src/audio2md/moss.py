from __future__ import annotations

from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
import math
import re
import subprocess
import tempfile
from typing import Any

from .model import Segment, SpeakerProfile
from .redimnet2 import extract_window_evidence, reconcile_speakers

MOSS_MODEL = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
MOSS_REVISION = "e8681d68e7042738ffca8ac8212bc8fcb1131ab8"
TARGET_PART_SECONDS = 30 * 60.0
WINDOW_OVERLAP_SECONDS = 2.0
SILENCE_SEARCH_SECONDS = 60.0
SILENCE_NOISE_DB = -35
SILENCE_MIN_SECONDS = 0.5
MAX_GENERATION_TOKENS = 65_536
SILENCE_START_RE = re.compile(r"silence_start: (?P<value>\d+(?:\.\d+)?)")
SILENCE_END_RE = re.compile(r"silence_end: (?P<value>\d+(?:\.\d+)?)")


def plan_windows(
    duration: float,
    *,
    silence_centers: list[float],
) -> list[tuple[float, float]]:
    if duration <= 0:
        return []
    part_count = math.ceil(duration / TARGET_PART_SECONDS)
    if part_count == 1:
        return [(0.0, duration)]

    ideal_part_seconds = duration / part_count
    boundaries = []
    for index in range(1, part_count):
        ideal = index * ideal_part_seconds
        nearby = [
            center
            for center in silence_centers
            if abs(center - ideal) <= SILENCE_SEARCH_SECONDS
        ]
        if not nearby:
            raise RuntimeError(
                f"no silence found within {SILENCE_SEARCH_SECONDS:.0f}s of "
                f"the ideal {ideal:.2f}s part boundary"
            )
        boundaries.append(min(nearby, key=lambda center: abs(center - ideal)))

    edges = [0.0, *boundaries, duration]
    half_overlap = WINDOW_OVERLAP_SECONDS / 2
    return [
        (
            max(0.0, start - (half_overlap if index else 0.0)),
            min(duration, end + (half_overlap if index < part_count - 1 else 0.0)),
        )
        for index, (start, end) in enumerate(zip(edges, edges[1:]))
    ]


def detect_silence_centers(path: Path) -> list[float]:
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "info", "-i", str(path),
            "-map", "0:a:0", "-af",
            f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_SECONDS}",
            "-f", "null", "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_silence_centers(result.stderr)


def parse_silence_centers(log: str) -> list[float]:
    centers = []
    start = None
    for line in log.splitlines():
        if match := SILENCE_START_RE.search(line):
            start = float(match.group("value"))
        elif start is not None and (match := SILENCE_END_RE.search(line)):
            centers.append((start + float(match.group("value"))) / 2)
            start = None
    return centers


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
    embedder: Any | None = None,
    speaker_profiles: dict[str, SpeakerProfile] | None = None,
) -> tuple[list[Segment], dict[str, Any], dict[str, SpeakerProfile]]:
    try:
        from mlx_audio.stt import load
    except ImportError as error:
        raise RuntimeError("MOSS runtime is unavailable; run audio2md through its locked environment") from error

    silence_centers = detect_silence_centers(path) if duration > TARGET_PART_SECONDS else []
    windows = plan_windows(duration, silence_centers=silence_centers)
    engine = load(MOSS_MODEL, revision=MOSS_REVISION)
    raw_windows: list[dict[str, Any]] = []
    by_window: list[list[Segment]] = []
    evidence_by_window = []
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
            evidence = {}
            embedding_diagnostics = []
            if role != "microphone":
                if embedder is None:
                    raise RuntimeError("ReDimNet2 embedder is required for non-microphone tracks")
                evidence, embedding_diagnostics = extract_window_evidence(
                    path,
                    segments,
                    window=index,
                    source_track=role,
                    embedder=embedder,
                )
            evidence_by_window.append(evidence)
            raw_windows.append({
                "index": index,
                "source_start": start,
                "source_end": end,
                "prompt_tokens": getattr(result, "prompt_tokens", None),
                "generation_tokens": getattr(result, "generation_tokens", None),
                "total_tokens": getattr(result, "total_tokens", None),
                "segments": [asdict(segment) for segment in segments],
                "embedding_samples": embedding_diagnostics,
            })

    mapping, decisions, profiles = reconcile_speakers(
        by_window,
        evidence_by_window,
        role=role,
        profiles=speaker_profiles,
    )
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
        "speaker_reconciliation": decisions,
        "window_strategy": "equal-silence-aligned",
        "silence_boundaries": boundaries_from_windows(windows),
        "actual_overlap_seconds": max(
            (
                max(0.0, windows[index - 1][1] - windows[index][0])
                for index in range(1, len(windows))
            ),
            default=0.0,
        ),
    }, profiles


def boundaries_from_windows(windows: list[tuple[float, float]]) -> list[float]:
    return [
        (windows[index - 1][1] + windows[index][0]) / 2
        for index in range(1, len(windows))
    ]


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
