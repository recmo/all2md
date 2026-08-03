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
MAX_GENERATION_TOKENS = 16_384
RECOVERY_TOKEN_THRESHOLD = 14_000
MAX_HOTWORDS = 40
COVERAGE_SLACK_SECONDS = 30.0
RECOVERY_OVERLAP_SECONDS = 30.0
MIN_RECOVERY_PROGRESS_SECONDS = 5.0
MAX_RECOVERY_ATTEMPTS = 8
# Independent reports of premature end tokens on long recordings:
# https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/issues/26
# https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/issues/34
ENGLISH_TRANSCRIPTION_PROMPT = (
    "Transcribe the audio. For each segment, start with the timestamp and "
    "speaker ID ([S01], [S02], [S03], ...), then the spoken text, and end "
    "with the segment timestamp."
)
SILENCE_START_RE = re.compile(r"silence_start: (?P<value>\d+(?:\.\d+)?)")
SILENCE_END_RE = re.compile(r"silence_end: (?P<value>\d+(?:\.\d+)?)")
TRANSCRIPT_START_RE = re.compile(
    r"\[(?P<start>\d+(?:\.\d+)?)\]\s*\[(?P<speaker>S\d+)\]"
)
TIMESTAMP_RE = re.compile(r"\[(?P<value>\d+(?:\.\d+)?)\]")


def normalize_hotwords(hotwords: list[str] | None) -> list[str]:
    normalized = []
    seen = set()
    for hotword in hotwords or []:
        value = hotword.strip()
        if not value:
            raise ValueError("hotwords must not be empty")
        if "\n" in value or "\r" in value:
            raise ValueError("hotwords must be single-line values")
        key = value.casefold()
        if key not in seen:
            normalized.append(value)
            seen.add(key)
    if len(normalized) > MAX_HOTWORDS:
        raise ValueError(f"at most {MAX_HOTWORDS} hotwords may be supplied")
    return normalized


def build_transcription_prompt(hotwords: list[str] | None = None) -> str:
    normalized = normalize_hotwords(hotwords)
    if not normalized:
        return ENGLISH_TRANSCRIPTION_PROMPT
    return f"{ENGLISH_TRANSCRIPTION_PROMPT} Hotwords: {', '.join(normalized)}"


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


def _parse_moss_transcript(text: str) -> tuple[list[dict[str, Any]], int]:
    starts = list(TRANSCRIPT_START_RE.finditer(text))
    segments = []
    rejected = 0
    for index, match in enumerate(starts):
        boundary = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        end_matches = list(TIMESTAMP_RE.finditer(text, match.end(), boundary))
        if not end_matches:
            rejected += 1
            continue
        end_match = end_matches[-1]
        if text[end_match.end():boundary].strip():
            rejected += 1
            continue
        start = float(match.group("start"))
        end = float(end_match.group("value"))
        segment_text = text[match.end():end_match.start()].strip()
        if end < start or not segment_text:
            rejected += 1
            continue
        segments.append({
            "start": start,
            "end": end,
            "speaker_id": match.group("speaker"),
            "text": segment_text,
        })
    return segments, rejected


def parse_moss_transcript(text: str) -> list[dict[str, Any]]:
    """Parse canonical MOSS output without treating numeric text as an end time."""
    return _parse_moss_transcript(text)[0]


def load_moss_engine() -> Any:
    try:
        from mlx_audio.stt import load
    except ImportError as error:
        raise RuntimeError(
            "MOSS runtime is unavailable; run audio2md through its locked environment"
        ) from error
    return load(MOSS_MODEL, revision=MOSS_REVISION)


def generation_diagnostics(result: Any) -> dict[str, Any]:
    raw_text = str(getattr(result, "text", ""))
    parsed, rejected = _parse_moss_transcript(raw_text)
    generation_tokens = getattr(result, "generation_tokens", None)
    possibly_truncated = (
        generation_tokens is not None
        and int(generation_tokens) >= MAX_GENERATION_TOKENS
    )
    if not raw_text.strip():
        parse_status = "empty"
    elif not parsed:
        parse_status = "invalid"
    elif rejected:
        parse_status = "partial"
    else:
        parse_status = "ok"
    return {
        "text": raw_text,
        "parsed": parsed,
        "generation_tokens": generation_tokens,
        "possibly_truncated": possibly_truncated,
        "parse_status": parse_status,
    }


def parse_segments(items: list[dict[str, Any]], *, window: int, offset: float, role: str) -> list[Segment]:
    segments = []
    for item in items or []:
        speaker_id = str(item.get("speaker_id", ""))
        if re.fullmatch(r"S\d+", speaker_id) is None:
            raise ValueError(f"invalid or missing MOSS speaker id: {speaker_id!r}")
        local_speaker = f"W{window:02d}:S{int(speaker_id[1:]):02d}"
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
    engine: Any,
    prompt: str,
    role: str,
    duration: float,
    embedder: Any | None = None,
    speaker_profiles: dict[str, SpeakerProfile] | None = None,
) -> tuple[list[Segment], dict[str, Any], dict[str, SpeakerProfile]]:
    silence_centers = detect_silence_centers(path) if duration > TARGET_PART_SECONDS else []
    planned_windows = plan_windows(duration, silence_centers=silence_centers)
    raw_windows: list[dict[str, Any]] = []
    by_window: list[list[Segment]] = []
    effective_windows: list[tuple[float, float]] = []
    evidence_by_window = []
    warnings = []
    with tempfile.TemporaryDirectory(prefix="audio2md-moss-") as temporary:
        directory = Path(temporary)
        inference_index = 0
        for planned_index, (planned_start, planned_end) in enumerate(planned_windows, 1):
            attempt = 1
            start = planned_start
            previous_coverage_end: float | None = None
            while True:
                inference_index += 1
                chunk = directory / f"window-{inference_index:03d}.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-v", "error", "-ss", str(start),
                        "-t", str(planned_end - start), "-i", str(path),
                        "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-y",
                        str(chunk),
                    ],
                    check=True,
                )
                result = engine.generate(
                    str(chunk),
                    max_tokens=MAX_GENERATION_TOKENS,
                    prompt=prompt,
                )
                diagnostics = generation_diagnostics(result)
                segments = parse_segments(
                    diagnostics["parsed"],
                    window=inference_index,
                    offset=start,
                    role=role,
                )
                coverage_end = min(
                    max((segment.end for segment in segments), default=start),
                    planned_end,
                )
                coverage_gap = max(0.0, planned_end - coverage_end)
                generation_tokens = diagnostics["generation_tokens"]
                token_count_suspect = (
                    generation_tokens is not None
                    and int(generation_tokens) >= RECOVERY_TOKEN_THRESHOLD
                )
                coverage_complete = (
                    coverage_gap <= COVERAGE_SLACK_SECONDS
                    and not token_count_suspect
                )
                if diagnostics["possibly_truncated"]:
                    warnings.append(
                        f"MOSS {role} inference {inference_index} reached the "
                        f"{MAX_GENERATION_TOKENS}-token generation ceiling"
                    )
                if diagnostics["parse_status"] == "invalid":
                    warnings.append(
                        f"MOSS {role} inference {inference_index} returned non-empty "
                        "output with no valid transcript segments"
                    )
                if diagnostics["parse_status"] == "partial":
                    warnings.append(
                        f"MOSS {role} inference {inference_index} returned partially "
                        "malformed output; some transcript segments were discarded"
                    )
                by_window.append(segments)
                effective_windows.append(
                    (start, planned_end if coverage_complete else coverage_end)
                )
                evidence = {}
                embedding_diagnostics = []
                if role != "microphone":
                    if embedder is None:
                        raise RuntimeError(
                            "ReDimNet2 embedder is required for non-microphone tracks"
                        )
                    evidence, embedding_diagnostics = extract_window_evidence(
                        path,
                        segments,
                        window=inference_index,
                        source_track=role,
                        embedder=embedder,
                    )
                evidence_by_window.append(evidence)
                raw_windows.append({
                    "index": inference_index,
                    "planned_window": planned_index,
                    "attempt": attempt,
                    "recovery": attempt > 1,
                    "source_start": start,
                    "source_end": planned_end,
                    "coverage_end": coverage_end,
                    "coverage_gap_seconds": coverage_gap,
                    "coverage_complete": coverage_complete,
                    "token_count_suspect": token_count_suspect,
                    "text": diagnostics["text"],
                    "prompt_tokens": getattr(result, "prompt_tokens", None),
                    "generation_tokens": generation_tokens,
                    "total_tokens": getattr(result, "total_tokens", None),
                    "possibly_truncated": diagnostics["possibly_truncated"],
                    "parse_status": diagnostics["parse_status"],
                    "segments": [asdict(segment) for segment in segments],
                    "embedding_samples": embedding_diagnostics,
                })
                if coverage_complete:
                    if attempt > 1:
                        warnings.append(
                            f"MOSS {role} planned window {planned_index} required "
                            f"{attempt - 1} overlapping recovery pass(es)"
                        )
                    break
                if not segments:
                    raise RuntimeError(
                        f"MOSS {role} planned window {planned_index} produced no "
                        "complete timestamped segment, so coverage cannot be verified"
                    )
                if (
                    previous_coverage_end is not None
                    and coverage_end - previous_coverage_end < MIN_RECOVERY_PROGRESS_SECONDS
                ):
                    raise RuntimeError(
                        f"MOSS {role} planned window {planned_index} made no meaningful "
                        f"coverage progress on recovery (stopped at {coverage_end:.2f}s)"
                    )
                if attempt > MAX_RECOVERY_ATTEMPTS:
                    raise RuntimeError(
                        f"MOSS {role} planned window {planned_index} remained "
                        f"unverified with a {coverage_gap:.2f}s timestamp gap after "
                        f"{MAX_RECOVERY_ATTEMPTS} recovery passes"
                    )
                previous_coverage_end = coverage_end
                start = max(planned_start, coverage_end - RECOVERY_OVERLAP_SECONDS)
                attempt += 1

    mapping, decisions, profiles = reconcile_speakers(
        by_window,
        evidence_by_window,
        role=role,
        profiles=speaker_profiles,
    )
    for segments in by_window:
        for segment in segments:
            segment.speaker = mapping.get(segment.speaker, segment.speaker)
    selected = trim_overlaps(by_window, effective_windows)
    selected = deduplicate_boundaries(selected)
    return selected, {
        "role": role,
        "audio": str(path),
        "prompt": prompt,
        "windows": raw_windows,
        "speaker_mapping": mapping,
        "speaker_reconciliation": decisions,
        "warnings": warnings,
        "window_strategy": "equal-silence-aligned",
        "silence_boundaries": boundaries_from_windows(planned_windows),
        "actual_overlap_seconds": max(
            (
                max(0.0, effective_windows[index - 1][1] - effective_windows[index][0])
                for index in range(1, len(effective_windows))
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
