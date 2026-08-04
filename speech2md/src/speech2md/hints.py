from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path

import yaml

from .model import AudioSource, ResolvedInput, SpeakerHint
from .moss import normalize_hotwords


@dataclass(frozen=True)
class SpeechHints:
    hotwords: tuple[str, ...] = ()
    speakers: tuple[SpeakerHint, ...] = ()
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
    _only(mapping, {"hotwords", "speakers"}, "hint sidecar")

    raw_hotwords = mapping.get("hotwords", [])
    if not isinstance(raw_hotwords, list):
        raise ValueError("hint hotwords must be a list")
    if not all(isinstance(item, str) for item in raw_hotwords):
        raise ValueError("hint hotwords must contain only strings")
    hotwords = tuple(normalize_hotwords(raw_hotwords))

    raw_speakers = mapping.get("speakers", [])
    if not isinstance(raw_speakers, list):
        raise ValueError("hint speakers must be a list")
    speakers: list[SpeakerHint] = []
    identities: set[str] = set()
    for speaker_index, raw_speaker in enumerate(raw_speakers, 1):
        speaker = _mapping(raw_speaker, f"hint speaker {speaker_index}")
        _only(speaker, {"identity", "ranges"}, f"hint speaker {speaker_index}")
        identity = speaker.get("identity")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError(f"hint speaker {speaker_index} identity must be a non-empty string")
        identity = identity.strip()
        if "\n" in identity or "\r" in identity:
            raise ValueError(f"hint speaker {speaker_index} identity must be a single-line string")
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
    return SpeechHints(hotwords, tuple(speakers), hashlib.sha256(raw).hexdigest())


def validate_hints(hints: SpeechHints, sources: list[AudioSource]) -> SpeechHints:
    if not hints.speakers:
        return hints
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
    return SpeechHints(hints.hotwords, tuple(validated), hints.sha256)


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
