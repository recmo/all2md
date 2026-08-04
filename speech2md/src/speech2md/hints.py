from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path

import yaml

from .model import AudioSource, ResolvedInput, Segment, SpeakerHint, TranscriptEdit
from .moss import normalize_hotwords


@dataclass(frozen=True)
class SpeechHints:
    hotwords: tuple[str, ...] = ()
    speakers: tuple[SpeakerHint, ...] = ()
    attendees: tuple[str, ...] = ()
    edits: tuple[TranscriptEdit, ...] = ()
    title: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    calendar_event: str | None = None
    sha256: str | None = None


def hint_path(resolved: ResolvedInput) -> Path:
    return resolved.markdown_path.with_suffix(".hint.yaml")


def load_hints(path: Path) -> SpeechHints:
    if not path.exists():
        return SpeechHints()
    if not path.is_file():
        raise ValueError(f"hint sidecar is not a file: {path}")
    raw = path.read_bytes()
    try:
        value = yaml.safe_load(raw.decode("utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid hint YAML: {error}") from error
    if value is None:
        value = {}
    mapping = _mapping(value, "hint sidecar")
    _only(
        mapping,
        {"attendees", "calendar_event", "edits", "ended_at", "hotwords", "speakers", "started_at", "title"},
        "hint sidecar",
    )
    title = _optional_single_line(mapping.get("title"), "hint title")
    started_at = _optional_single_line(mapping.get("started_at"), "hint started_at")
    ended_at = _optional_single_line(mapping.get("ended_at"), "hint ended_at")
    calendar_event = _optional_single_line(mapping.get("calendar_event"), "hint calendar_event")
    if calendar_event and not calendar_event.startswith(("https://", "http://")):
        raise ValueError("hint calendar_event must be an http(s) URL")

    raw_hotwords = mapping.get("hotwords", [])
    if not isinstance(raw_hotwords, list):
        raise ValueError("hint hotwords must be a list")
    if not all(isinstance(item, str) for item in raw_hotwords):
        raise ValueError("hint hotwords must contain only strings")
    hotwords = tuple(normalize_hotwords(raw_hotwords))

    raw_attendees = mapping.get("attendees", [])
    if not isinstance(raw_attendees, list):
        raise ValueError("hint attendees must be a list")
    attendees: list[str] = []
    for attendee_index, raw_attendee in enumerate(raw_attendees, 1):
        identity = _single_line(raw_attendee, f"hint attendee {attendee_index}")
        if identity in attendees:
            raise ValueError(f"duplicate hint attendee identity: {identity}")
        attendees.append(identity)

    raw_speakers = mapping.get("speakers", [])
    if not isinstance(raw_speakers, list):
        raise ValueError("hint speakers must be a list")
    speakers: list[SpeakerHint] = []
    identities: set[str] = set()
    for speaker_index, raw_speaker in enumerate(raw_speakers, 1):
        speaker = _mapping(raw_speaker, f"hint speaker {speaker_index}")
        _only(speaker, {"identity", "ranges"}, f"hint speaker {speaker_index}")
        identity = _single_line(speaker.get("identity"), f"hint speaker {speaker_index} identity")
        if identity in identities:
            raise ValueError(f"duplicate hint speaker identity: {identity}")
        identities.add(identity)
        raw_ranges = speaker.get("ranges")
        if not isinstance(raw_ranges, list) or not raw_ranges:
            raise ValueError(f"hint speaker {identity} ranges must be a non-empty list")
        for range_index, raw_range in enumerate(raw_ranges, 1):
            label = f"hint range {identity} #{range_index}"
            range_value = _mapping(raw_range, label)
            _only(range_value, {"track", "start", "end"}, label)
            start = _number(range_value.get("start"), f"{label} start")
            end = _number(range_value.get("end"), f"{label} end")
            if start < 0 or end <= start:
                raise ValueError(f"{label} must have 0 <= start < end")
            track = range_value.get("track")
            if track is not None and (not isinstance(track, str) or not track.strip()):
                raise ValueError(f"{label} track must be a non-empty string")
            speakers.append(SpeakerHint(identity, start, end, track.strip() if track else None))
    raw_edits = mapping.get("edits", [])
    if not isinstance(raw_edits, list):
        raise ValueError("hint edits must be a list")
    edits: list[TranscriptEdit] = []
    for edit_index, raw_edit in enumerate(raw_edits, 1):
        label = f"hint edit {edit_index}"
        edit = _mapping(raw_edit, label)
        _only(edit, {"track", "start", "end", "before", "after"}, label)
        start = _number(edit.get("start"), f"{label} start")
        end = _number(edit.get("end"), f"{label} end")
        if start < 0 or end <= start:
            raise ValueError(f"{label} must have 0 <= start < end")
        before = _single_line(edit.get("before"), f"{label} before")
        after = _single_line(edit.get("after"), f"{label} after")
        track = edit.get("track")
        if track is not None:
            track = _single_line(track, f"{label} track")
        edits.append(TranscriptEdit(start, end, before, after, track))
    return SpeechHints(
        hotwords=hotwords,
        speakers=tuple(speakers),
        attendees=tuple(attendees),
        edits=tuple(edits),
        title=title,
        started_at=started_at,
        ended_at=ended_at,
        calendar_event=calendar_event,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def validate_hints(hints: SpeechHints, sources: list[AudioSource]) -> SpeechHints:
    by_role: dict[str, list[AudioSource]] = {}
    for source in sources:
        by_role.setdefault(source.role, []).append(source)
    validated: list[SpeakerHint] = []
    for item in hints.speakers:
        if item.track is None:
            if len(sources) != 1:
                raise ValueError(
                    f"hint range {item.identity} at {item.start:g}-{item.end:g}s requires a track"
                )
            track = sources[0].role
            source = sources[0]
        else:
            matches = by_role.get(item.track, [])
            if not matches:
                raise ValueError(f"unknown hint track: {item.track}")
            if len(matches) != 1:
                raise ValueError(f"ambiguous hint track: {item.track}")
            track = item.track
            source = matches[0]
        if item.end > source.duration_seconds:
            raise ValueError(
                f"hint range {item.identity} at {item.start:g}-{item.end:g}s exceeds "
                f"{track} duration {source.duration_seconds:g}s"
            )
        validated.append(SpeakerHint(item.identity, item.start, item.end, track))

    for index, left in enumerate(validated):
        for right in validated[index + 1 :]:
            if (
                left.track == right.track
                and left.identity != right.identity
                and min(left.end, right.end) > max(left.start, right.start)
            ):
                raise ValueError(
                    f"conflicting hint ranges for {left.identity} and {right.identity} "
                    f"on {left.track}"
                )
    for edit in hints.edits:
        if edit.track is None:
            if len(sources) != 1:
                raise ValueError(f"hint edit at {edit.start:g}-{edit.end:g}s requires a track")
            source = sources[0]
        else:
            matches = by_role.get(edit.track, [])
            if not matches:
                raise ValueError(f"unknown hint track: {edit.track}")
            if len(matches) != 1:
                raise ValueError(f"ambiguous hint track: {edit.track}")
            source = matches[0]
        if edit.end > source.duration_seconds:
            raise ValueError(
                f"hint edit at {edit.start:g}-{edit.end:g}s exceeds "
                f"{source.role} duration {source.duration_seconds:g}s"
            )
    return SpeechHints(
        hotwords=hints.hotwords,
        speakers=tuple(validated),
        attendees=hints.attendees,
        edits=hints.edits,
        title=hints.title,
        started_at=hints.started_at,
        ended_at=hints.ended_at,
        calendar_event=hints.calendar_event,
        sha256=hints.sha256,
    )


def apply_edits(segments: list[Segment], edits: tuple[TranscriptEdit, ...]) -> None:
    for edit in edits:
        matches: list[Segment] = []
        for segment in segments:
            if edit.track is not None and segment.source_role != edit.track:
                continue
            if min(segment.end, edit.end) <= max(segment.start, edit.start):
                continue
            if edit.before in segment.text:
                matches.append(segment)
        occurrences = sum(segment.text.count(edit.before) for segment in matches)
        if occurrences != 1:
            raise ValueError(
                f"hint edit at {edit.start:g}-{edit.end:g}s matched {occurrences} occurrences"
            )
        segment = matches[0]
        segment.text = segment.text.replace(edit.before, edit.after, 1)


def _mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return value


def _only(value: dict, allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown field(s): {', '.join(sorted(unknown))}")


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _single_line(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a single-line string")
    return value


def _optional_single_line(value, label: str) -> str | None:
    if value is None:
        return None
    return _single_line(value, label)
