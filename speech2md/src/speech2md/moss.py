from __future__ import annotations

from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
import math
import re
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Any, Callable

from .model import Segment, SpeakerHint, SpeakerProfile
from .redimnet2 import (
    extract_window_evidence,
    reconcile_speaker_runs,
    split_speaker_runs,
)

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
RECOVERY_OVERLAP_SECONDS = 30.0
MIN_RECOVERY_PROGRESS_SECONDS = 5.0
MAX_RECOVERY_ATTEMPTS = 8
SPEAKER_FORCE_TOLERANCE_SECONDS = 0.5
MOSS_TIMESTAMP_TOLERANCE_SECONDS = 0.02
FRESH_SPEAKER_MIN_PROBABILITY = 0.10
FRESH_SPEAKER_MIN_TOP_RATIO = 0.10
RUN_VOICEPRINT_SAMPLES = 1
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
SPEAKER_PREFIX_RE = re.compile(r"\[(?P<start>\d+(?:\.\d+)?)\]\[S$")
SPEAKER_DIGIT_RE = re.compile(r"\[(?P<start>\d+(?:\.\d+)?)\]\[S0$")
SPEAKER_LABEL_RE = re.compile(r"\[S(?P<number>\d+)\]")


class MossGuidanceRequired(RuntimeError):
    """A cached window needs model decoding to satisfy speaker hints."""


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
            "MOSS runtime is unavailable; run speech2md through its locked environment"
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


def _generate_with_timestamp_progress(
    engine: Any,
    audio: str,
    *,
    prompt: str,
    timestamp_callback: Callable[[float], None] | None = None,
    speaker_forces: list[dict[str, Any]] | None = None,
) -> Any:
    if speaker_forces:
        return _generate_with_speaker_forces(
            engine,
            audio,
            prompt=prompt,
            timestamp_callback=timestamp_callback,
            speaker_forces=speaker_forces,
        )
    if hasattr(engine, "stream_generate"):
        return _generate_with_speaker_forces(
            engine,
            audio,
            prompt=prompt,
            timestamp_callback=timestamp_callback,
            speaker_forces=[],
        )
    generated = engine.generate(
        audio,
        max_tokens=MAX_GENERATION_TOKENS,
        prompt=prompt,
        stream=True,
    )
    try:
        stream = iter(generated)
    except TypeError:
        # Test doubles and older runtimes may still return a completed result.
        return generated

    text_parts = []
    timestamp_tail = ""
    generation_tokens = 0
    for update in stream:
        text = str(getattr(update, "text", ""))
        text_parts.append(text)
        previous_tail_length = len(timestamp_tail)
        timestamp_text = timestamp_tail + text
        new_timestamps = [
            match
            for match in TIMESTAMP_RE.finditer(timestamp_text)
            if match.end() > previous_tail_length
        ]
        timestamp_tail = timestamp_text[-64:]
        generation_tokens = max(
            generation_tokens,
            int(getattr(update, "generation_tokens", 0) or 0),
        )
        if timestamp_callback is not None:
            for match in new_timestamps:
                timestamp_callback(float(match.group("value")))

    return SimpleNamespace(
        text="".join(text_parts).strip(),
        prompt_tokens=None,
        generation_tokens=generation_tokens,
        total_tokens=None,
    )


def _speaker_decision(
    probabilities: dict[str, float],
    used_speakers: set[str],
) -> tuple[dict[str, Any], str | None]:
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    top, alternative = ranked[:2]
    fresh = next(
        (f"S{number:02d}" for number in range(1, 10) if f"S{number:02d}" not in used_speakers),
        None,
    )
    alternative_is_fresh = alternative[0] == fresh
    ratio = alternative[1] / top[1] if top[1] else 0.0
    force = (
        alternative[0]
        if top[0] in used_speakers
        and alternative_is_fresh
        and alternative[1] >= FRESH_SPEAKER_MIN_PROBABILITY
        and ratio >= FRESH_SPEAKER_MIN_TOP_RATIO
        else None
    )
    return {
        "top_candidate": top[0],
        "top_probability": round(top[1], 8),
        "alternative_candidate": alternative[0],
        "alternative_probability": round(alternative[1], 8),
        "alternative_is_fresh": alternative_is_fresh,
        "fresh_candidate": fresh,
        "alternative_top_ratio": round(ratio, 8),
    }, force


def _generate_with_speaker_forces(
    engine: Any,
    audio: str,
    *,
    prompt: str,
    timestamp_callback: Callable[[float], None] | None,
    speaker_forces: list[dict[str, Any]],
) -> Any:
    try:
        import mlx.core as mx
    except ImportError as error:
        raise RuntimeError("MOSS speaker guidance requires the MLX runtime") from error

    tokenizer = engine._tokenizer
    pending: list[int] = []
    applied: list[dict[str, Any]] = []
    remaining = list(speaker_forces)
    decisions: list[dict[str, Any]] = []
    used_speakers: set[str] = set()
    active_force: dict[str, Any] | None = None
    digit_ids = {
        f"S0{digit}": tokenizer.encode(str(digit), add_special_tokens=False)[0]
        for digit in range(1, 10)
    }

    def speaker_probabilities(logits) -> dict[str, float]:
        values = {
            label: float(logits[0, token_id] if logits.ndim > 1 else logits[token_id])
            for label, token_id in digit_ids.items()
        }
        peak = max(values.values())
        denominator = sum(math.exp(value - peak) for value in values.values())
        return {
            label: math.exp(value - peak) / denominator
            for label, value in values.items()
        }

    def force_speaker(tokens, logits):
        nonlocal active_force, pending
        tail = tokenizer.decode(
            [int(token) for token in tokens[-64:].tolist()],
            skip_special_tokens=True,
        )
        used_speakers.update(
            f"S{int(match.group('number')):02d}"
            for match in SPEAKER_LABEL_RE.finditer(tail)
        )
        if not pending:
            match = SPEAKER_PREFIX_RE.search(tail)
            if match is not None:
                start = float(match.group("start"))
                force = min(
                    remaining,
                    key=lambda item: abs(float(item["start"]) - start),
                    default=None,
                )
                if force is not None and (
                    abs(float(force["start"]) - start) > SPEAKER_FORCE_TOLERANCE_SECONDS
                ):
                    force = None
                if force is not None:
                    full = tokenizer.encode(
                        f"[{force['speaker']}]",
                        add_special_tokens=False,
                    )
                    prefix = tokenizer.encode("[S", add_special_tokens=False)
                    if full[:len(prefix)] != prefix:
                        raise RuntimeError("MOSS tokenizer has an unsupported speaker-token shape")
                    pending = [int(token) for token in full[len(prefix):]]
                    remaining.remove(force)
                    active_force = dict(force)
                    applied.append(active_force)
        digit_match = SPEAKER_DIGIT_RE.search(tail)
        if digit_match is not None:
            probabilities = speaker_probabilities(logits)
            decision, novelty_force = _speaker_decision(probabilities, used_speakers)
            decision["start"] = float(digit_match.group("start"))
            if active_force is not None:
                decision["forced_candidate"] = active_force["speaker"]
                decision["force_reason"] = "guidance"
            elif novelty_force is not None:
                full = tokenizer.encode(f"[{novelty_force}]", add_special_tokens=False)
                prefix = tokenizer.encode("[S0", add_special_tokens=False)
                if full[:len(prefix)] != prefix:
                    raise RuntimeError("MOSS tokenizer has an unsupported speaker-token shape")
                pending = [int(token) for token in full[len(prefix):]]
                decision["forced_candidate"] = novelty_force
                decision["force_reason"] = "fresh_alternative"
            decisions.append(decision)
        if not pending:
            return logits
        target = pending.pop(0)
        if not pending:
            active_force = None
        mask = mx.arange(logits.shape[-1])[None, :] == target
        return mx.where(mask, logits, mx.array(-float("inf"), dtype=logits.dtype))

    text_parts = []
    timestamp_tail = ""
    generation_tokens = 0
    for token, _ in engine.stream_generate(
        audio,
        max_tokens=MAX_GENERATION_TOKENS,
        prompt=prompt,
        logits_processors=[force_speaker],
    ):
        generation_tokens += 1
        text = tokenizer.decode([int(token)], skip_special_tokens=True)
        text_parts.append(text)
        previous_tail_length = len(timestamp_tail)
        timestamp_text = timestamp_tail + text
        new_timestamps = [
            match
            for match in TIMESTAMP_RE.finditer(timestamp_text)
            if match.end() > previous_tail_length
        ]
        timestamp_tail = timestamp_text[-64:]
        if timestamp_callback is not None:
            for match in new_timestamps:
                timestamp_callback(float(match.group("value")))

    if remaining:
        missed = ", ".join(f"{item['start']:g}s" for item in remaining)
        raise RuntimeError(f"MOSS did not emit forced speaker boundary/boundaries at {missed}")
    return SimpleNamespace(
        text="".join(text_parts).strip(),
        prompt_tokens=None,
        generation_tokens=generation_tokens,
        total_tokens=None,
        speaker_forces_applied=applied,
        speaker_decisions=decisions,
    )


def _select_hint_segments(
    segments: list[Segment],
    hints: tuple[SpeakerHint, ...],
    *,
    role: str,
) -> tuple[list[tuple[SpeakerHint, list[Segment]]], list[SpeakerHint]]:
    def prefer_dominant_rounded_start(
        hint: SpeakerHint,
        selected: list[Segment],
    ) -> list[Segment]:
        if abs(hint.start - round(hint.start)) > 1e-6:
            return selected
        starting_speakers = {
            segment.speaker
            for segment in selected
            if hint.start <= segment.start < hint.start + 1.0
        }
        if len(starting_speakers) < 2:
            return selected
        overlaps = {
            speaker: sum(
                max(0.0, min(segment.end, hint.end) - max(segment.start, hint.start))
                for segment in selected
                if segment.speaker == speaker
            )
            for speaker in {segment.speaker for segment in selected}
        }
        best_overlap = max(overlaps.values())
        best_speakers = [
            speaker for speaker, overlap in overlaps.items()
            if abs(overlap - best_overlap) <= 1e-6
        ]
        if len(best_speakers) != 1:
            return selected
        return [
            segment for segment in selected
            if segment.speaker == best_speakers[0]
        ]

    relevant_hints = [hint for hint in hints if hint.track == role]
    assigned: dict[SpeakerHint, list[Segment]] = {}
    for segment in segments:
        midpoint = (segment.start + segment.end) / 2
        centered = [
            hint for hint in relevant_hints
            if hint.start <= midpoint < hint.end
        ]
        if centered:
            assigned.setdefault(centered[0], []).append(segment)

    assigned = {
        hint: prefer_dominant_rounded_start(hint, selected)
        for hint, selected in assigned.items()
    }

    assigned_segments = {id(segment) for selected in assigned.values() for segment in selected}
    for hint in relevant_hints:
        if hint in assigned:
            continue
        overlapping = [
            segment for segment in segments
            if id(segment) not in assigned_segments
            and min(segment.end, hint.end) > max(segment.start, hint.start)
        ]
        if not overlapping:
            continue
        overlaps = {
            id(segment): min(segment.end, hint.end) - max(segment.start, hint.start)
            for segment in overlapping
        }
        best_overlap = max(overlaps.values())
        selected = [
            segment for segment in overlapping
            if abs(overlaps[id(segment)] - best_overlap) <= 1e-6
        ]
        selected = prefer_dominant_rounded_start(hint, selected)
        assigned[hint] = selected
        assigned_segments.update(id(segment) for segment in selected)

    unassigned = [hint for hint in relevant_hints if hint not in assigned]
    return list(assigned.items()), unassigned


def plan_speaker_forces(
    segments: list[Segment],
    hints: tuple[SpeakerHint, ...],
    *,
    role: str,
    offset: float,
) -> list[dict[str, Any]]:
    selections, _ = _select_hint_segments(segments, hints, role=role)

    label_identities: dict[str, set[str]] = {}
    scores: dict[tuple[str, str], float] = {}
    first_occurrence: dict[tuple[str, str], float] = {}
    for hint, selected in selections:
        for segment in selected:
            label_identities.setdefault(segment.speaker, set()).add(hint.identity)
            overlap = max(0.0, min(segment.end, hint.end) - max(segment.start, hint.start))
            key = (segment.speaker, hint.identity)
            scores[key] = scores.get(key, 0.0) + overlap
            first_occurrence[key] = min(first_occurrence.get(key, math.inf), segment.start)

    collisions = {
        label: identities
        for label, identities in label_identities.items()
        if len(identities) > 1
    }
    if not collisions:
        return []

    allocated: dict[str, str] = {}
    allocated_numbers: set[int] = set()

    def unused_label(window_label: str, before: float) -> str:
        window = window_label.rsplit(":", 1)[0]
        used_numbers = {
            int(match.group(1))
            for segment in segments
            if segment.start < before
            and (match := re.search(r":S(\d+)$", segment.speaker))
        } | allocated_numbers
        number = 1
        while number in used_numbers:
            number += 1
        allocated_numbers.add(number)
        return f"{window}:S{number:02d}"

    replacements: dict[tuple[str, str], str] = {}
    for label, identities in sorted(collisions.items()):
        ordered_identities = sorted(
            identities,
            key=lambda identity: first_occurrence[(label, identity)],
        )
        owner = ordered_identities[0]
        for identity in ordered_identities:
            if identity == owner:
                target = label
            else:
                target = allocated.get(identity)
                if target is None:
                    target = unused_label(
                        label,
                        first_occurrence[(label, identity)],
                    )
                    allocated[identity] = target
            replacements[(label, identity)] = target

    forces: list[dict[str, Any]] = []
    seen: set[tuple[float, str]] = set()
    for hint, selected in selections:
        for segment in selected:
            target = replacements.get((segment.speaker, hint.identity))
            if target is None or target == segment.speaker:
                continue
            start = round(segment.start - offset, 2)
            speaker = target.rsplit(":", 1)[-1]
            key = (start, speaker)
            if key in seen:
                continue
            seen.add(key)
            forces.append({"start": start, "speaker": speaker, "identity": hint.identity})
    return sorted(forces, key=lambda item: (item["start"], item["speaker"], item["identity"]))


def _force_signature(forces: list[dict[str, Any]]) -> list[tuple[float, str]]:
    return sorted(
        (round(float(force["start"]), 2), str(force["speaker"]))
        for force in forces
    )


def _generation_fields(result: Any) -> dict[str, Any]:
    fields = {
        "text": str(getattr(result, "text", "")),
        "prompt_tokens": getattr(result, "prompt_tokens", None),
        "generation_tokens": getattr(result, "generation_tokens", None),
        "total_tokens": getattr(result, "total_tokens", None),
    }
    decisions = getattr(result, "speaker_decisions", None)
    if decisions is not None:
        fields["speaker_decisions"] = decisions
    return fields


def parse_segments(
    items: list[dict[str, Any]],
    *,
    window: int,
    offset: float,
    duration: float,
    role: str,
) -> list[Segment]:
    segments = []
    for item in items or []:
        speaker_id = str(item.get("speaker_id", ""))
        if re.fullmatch(r"S\d+", speaker_id) is None:
            raise ValueError(f"invalid or missing MOSS speaker id: {speaker_id!r}")
        relative_start = float(item["start"])
        relative_end = float(item["end"])
        if (
            relative_start < 0
            or relative_end < relative_start
            or relative_start > duration
            or relative_end > duration + MOSS_TIMESTAMP_TOLERANCE_SECONDS
        ):
            raise ValueError(
                f"MOSS segment [{relative_start}, {relative_end}] falls outside "
                f"the submitted {duration:.2f}s audio span"
            )
        relative_end = min(relative_end, duration)
        local_speaker = f"W{window:02d}:S{int(speaker_id[1:]):02d}"
        text = str(item.get("text", "")).removeprefix(f"[{speaker_id}]").strip()
        if text:
            segments.append(Segment(
                start=offset + relative_start,
                end=offset + relative_end,
                text=text,
                speaker=local_speaker,
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
    speaker_hints: tuple[SpeakerHint, ...] = (),
    cached_generations: list[dict[str, Any]] | None = None,
    generation_cache_callback: Callable[[list[dict[str, Any]]], None] | None = None,
    progress_callback: Callable[[int, int, int, float], None] | None = None,
) -> tuple[list[Segment], dict[str, Any], dict[str, SpeakerProfile]]:
    silence_centers = detect_silence_centers(path) if duration > TARGET_PART_SECONDS else []
    planned_windows = plan_windows(duration, silence_centers=silence_centers)
    raw_windows: list[dict[str, Any]] = []
    by_window: list[list[Segment]] = []
    effective_windows: list[tuple[float, float]] = []
    evidence_by_window = []
    warnings = []
    generation_cache: list[dict[str, Any]] = []
    cached_iterator = iter(cached_generations) if cached_generations is not None else None
    reported_through = 0.0
    with tempfile.TemporaryDirectory(prefix="speech2md-moss-") as temporary:
        directory = Path(temporary)
        inference_index = 0
        for planned_index, (planned_start, planned_end) in enumerate(planned_windows, 1):
            attempt = 1
            start = planned_start
            previous_coverage_end: float | None = None
            while True:
                inference_index += 1
                if progress_callback is not None:
                    progress_callback(
                        planned_index,
                        len(planned_windows),
                        attempt,
                        0.0,
                    )
                chunk = directory / f"window-{inference_index:03d}.wav"

                def report_timestamp(relative_seconds: float) -> None:
                    nonlocal reported_through
                    position = min(planned_end, start + relative_seconds)
                    newly_reported = max(0.0, position - reported_through)
                    if newly_reported and progress_callback is not None:
                        reported_through = position
                        progress_callback(
                            planned_index,
                            len(planned_windows),
                            attempt,
                            newly_reported,
                        )

                def extract_chunk() -> None:
                    subprocess.run(
                        [
                            "ffmpeg", "-nostdin", "-v", "error", "-ss", str(start),
                            "-t", str(planned_end - start), "-i", str(path),
                            "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-y",
                            str(chunk),
                        ],
                        check=True,
                    )

                if cached_iterator is None:
                    extract_chunk()
                    base_result = _generate_with_timestamp_progress(
                        engine,
                        str(chunk),
                        prompt=prompt,
                        timestamp_callback=report_timestamp,
                    )
                    base_fields = _generation_fields(base_result)
                    base_diagnostics = generation_diagnostics(base_result)
                    base_segments = parse_segments(
                        base_diagnostics["parsed"],
                        window=inference_index,
                        offset=start,
                        duration=planned_end - start,
                        role=role,
                    )
                    speaker_forces = plan_speaker_forces(
                        base_segments,
                        speaker_hints,
                        role=role,
                        offset=start,
                    )
                    if speaker_forces:
                        result = _generate_with_timestamp_progress(
                            engine,
                            str(chunk),
                            prompt=prompt,
                            timestamp_callback=report_timestamp,
                            speaker_forces=speaker_forces,
                        )
                    else:
                        result = base_result
                else:
                    try:
                        cached_result = next(cached_iterator)
                    except StopIteration as error:
                        raise ValueError("MOSS cache ended before transcription completed") from error
                    if not isinstance(cached_result, dict):
                        raise ValueError("MOSS cache contains an invalid generation")
                    base_fields = cached_result.get("base", cached_result)
                    if not isinstance(base_fields, dict):
                        raise ValueError("MOSS cache contains an invalid base generation")
                    base_result = SimpleNamespace(**base_fields)
                    base_diagnostics = generation_diagnostics(base_result)
                    base_segments = parse_segments(
                        base_diagnostics["parsed"],
                        window=inference_index,
                        offset=start,
                        duration=planned_end - start,
                        role=role,
                    )
                    speaker_forces = plan_speaker_forces(
                        base_segments,
                        speaker_hints,
                        role=role,
                        offset=start,
                    )
                    cached_forces = cached_result.get("speaker_forces", [])
                    if not isinstance(cached_forces, list):
                        raise ValueError("MOSS cache contains invalid speaker guidance")
                    if not speaker_forces:
                        result = base_result
                    elif _force_signature(cached_forces) == _force_signature(speaker_forces):
                        result = SimpleNamespace(**cached_result)
                    else:
                        if engine is None:
                            raise MossGuidanceRequired(
                                f"MOSS {role} inference {inference_index} needs guided decoding"
                            )
                        extract_chunk()
                        result = _generate_with_timestamp_progress(
                            engine,
                            str(chunk),
                            prompt=prompt,
                            timestamp_callback=report_timestamp,
                            speaker_forces=speaker_forces,
                        )
                active_fields = _generation_fields(result)
                cache_entry = dict(active_fields)
                if speaker_forces:
                    cache_entry["base"] = dict(base_fields)
                    cache_entry["speaker_forces"] = speaker_forces
                generation_cache.append(cache_entry)
                diagnostics = generation_diagnostics(result)
                segments = parse_segments(
                    diagnostics["parsed"],
                    window=inference_index,
                    offset=start,
                    duration=planned_end - start,
                    role=role,
                )
                coverage_end = max((segment.end for segment in segments), default=start)
                coverage_gap = max(0.0, planned_end - coverage_end)
                generation_tokens = diagnostics["generation_tokens"]
                token_count_suspect = (
                    generation_tokens is not None
                    and int(generation_tokens) >= RECOVERY_TOKEN_THRESHOLD
                )
                requires_recovery = token_count_suspect
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
                    (start, coverage_end if requires_recovery else planned_end)
                )
                raw_windows.append({
                    "index": inference_index,
                    "planned_window": planned_index,
                    "attempt": attempt,
                    "recovery": attempt > 1,
                    "source_start": start,
                    "source_end": planned_end,
                    "coverage_end": coverage_end,
                    "coverage_gap_seconds": coverage_gap,
                    "token_count_suspect": token_count_suspect,
                    "requires_recovery": requires_recovery,
                    "text": diagnostics["text"],
                    "prompt_tokens": getattr(result, "prompt_tokens", None),
                    "generation_tokens": generation_tokens,
                    "total_tokens": getattr(result, "total_tokens", None),
                    "possibly_truncated": diagnostics["possibly_truncated"],
                    "parse_status": diagnostics["parse_status"],
                    "segments": [asdict(segment) for segment in segments],
                    "embedding_samples": [],
                })
                if diagnostics["parse_status"] == "invalid":
                    raise RuntimeError(
                        f"MOSS {role} inference {inference_index} returned non-empty "
                        "output with no valid transcript segments"
                    )
                if not requires_recovery:
                    if attempt > 1:
                        warnings.append(
                            f"MOSS {role} planned window {planned_index} required "
                            f"{attempt - 1} overlapping recovery pass(es)"
                        )
                    newly_completed = max(0.0, planned_end - reported_through)
                    reported_through = max(reported_through, planned_end)
                    if progress_callback is not None:
                        progress_callback(
                            planned_index,
                            len(planned_windows),
                            attempt,
                            newly_completed,
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
                        f"MOSS {role} planned window {planned_index} remained token-"
                        f"suspect after {MAX_RECOVERY_ATTEMPTS} recovery passes "
                        f"({generation_tokens} tokens in the latest pass)"
                    )
                previous_coverage_end = coverage_end
                start = max(planned_start, coverage_end - RECOVERY_OVERLAP_SECONDS)
                attempt += 1

    if cached_iterator is not None:
        try:
            next(cached_iterator)
        except StopIteration:
            pass
        else:
            raise ValueError("MOSS cache has unused generations")

    if generation_cache_callback is not None:
        generation_cache_callback(list(generation_cache))

    run_windows, original_labels = split_speaker_runs(by_window)
    for index, segments in enumerate(run_windows, 1):
        evidence = {}
        if embedder is not None:
            evidence, embedding_diagnostics = extract_window_evidence(
                path,
                segments,
                window=index,
                source_track=role,
                embedder=embedder,
                maximum_samples_per_speaker=RUN_VOICEPRINT_SAMPLES,
            )
            raw_windows[index - 1]["embedding_samples"] = embedding_diagnostics
        evidence_by_window.append(evidence)

    selected_for_hints = trim_overlaps(run_windows, effective_windows)
    anchors = resolve_speaker_anchors(selected_for_hints, speaker_hints, role=role)

    mapping, decisions, profiles = reconcile_speaker_runs(
        run_windows,
        evidence_by_window,
        original_labels=original_labels,
        role=role,
        profiles=speaker_profiles,
        anchors=anchors,
    )
    for segments in run_windows:
        for segment in segments:
            segment.speaker = mapping.get(segment.speaker, segment.speaker)
    selected = trim_overlaps(run_windows, effective_windows)
    selected = deduplicate_boundaries(selected)
    return selected, {
        "role": role,
        "audio": str(path),
        "prompt": prompt,
        "windows": raw_windows,
        "speaker_mapping": mapping,
        "speaker_run_origins": original_labels,
        "speaker_reconciliation": decisions,
        "warnings": warnings,
        "generation_cache": generation_cache,
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


def resolve_speaker_anchors(
    segments: list[Segment],
    hints: tuple[SpeakerHint, ...],
    *,
    role: str,
) -> dict[str, str]:
    anchors: dict[str, str] = {}
    selections, unassigned = _select_hint_segments(segments, hints, role=role)
    for hint, selected in selections:
        speakers = {segment.speaker for segment in selected}
        if len(speakers) == 1:
            selected_speakers = speakers
        elif all(
            segment.start >= hint.start - 1e-6
            and segment.end <= hint.end + 1e-6
            for segment in selected
        ):
            selected_speakers = speakers
        else:
            overlap_by_speaker = {
                speaker: sum(
                    max(0.0, min(segment.end, hint.end) - max(segment.start, hint.start))
                    for segment in selected
                    if segment.speaker == speaker
                )
                for speaker in speakers
            }
            best_overlap = max(overlap_by_speaker.values())
            best_speakers = [
                speaker
                for speaker, overlap in overlap_by_speaker.items()
                if abs(overlap - best_overlap) <= 1e-6
            ]
            if len(best_speakers) != 1:
                raise ValueError(
                    f"speaker hint {hint.identity} at {hint.start:g}-{hint.end:g}s "
                    f"on {role} overlaps multiple diarized speakers"
                )
            selected_speakers = set(best_speakers)
        for speaker in selected_speakers:
            previous = anchors.get(speaker)
            if previous is not None and previous != hint.identity:
                raise ValueError(
                    f"diarized speaker {speaker} is anchored to both "
                    f"{previous} and {hint.identity}"
                )
            anchors[speaker] = hint.identity
    for hint in unassigned:
        if hint.identity in anchors.values():
            continue
        if not any(
            min(segment.end, hint.end) > max(segment.start, hint.start)
            for segment in segments
        ):
            raise ValueError(
                f"speaker hint {hint.identity} at {hint.start:g}-{hint.end:g}s "
                f"on {role} does not overlap speech"
            )
        raise ValueError(
            f"speaker hint {hint.identity} at {hint.start:g}-{hint.end:g}s "
            f"on {role} cannot be separated from an adjacent hinted range"
        )
    return anchors


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
