from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AudioSource:
    path: str
    role: str
    sha256: str
    duration_seconds: float
    format: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str
    source_role: str


@dataclass
class EmbeddingSample:
    vector: list[float]
    source_track: str
    start: float
    end: float
    duration_seconds: float
    window: int
    quality: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpeakerProfile:
    speaker: str
    model: str
    model_revision: str
    checkpoint_sha256: str
    embedding_dimension: int
    samples: list[EmbeddingSample] = field(default_factory=list)
    identity: str = ""


@dataclass(frozen=True)
class SpeakerHint:
    identity: str
    start: float
    end: float
    track: str | None = None


@dataclass(frozen=True)
class TranscriptEdit:
    start: float
    end: float
    before: str
    after: str
    track: str | None = None


@dataclass
class TranscriptState:
    title: str | None
    started_at: str | None
    processing_seconds: float
    segments: list[Segment]
    source_sha256: str
    ended_at: str | None = None
    calendar_event: str | None = None
    attendees: list[dict[str, str]] = field(default_factory=list)
    speaker_names: dict[str, str] = field(default_factory=dict)
    hints_sha256: str | None = None


@dataclass(frozen=True)
class ResolvedInput:
    requested: Path
    markdown_path: Path
    title: str | None
    started_at: str | None
    ended_at: str | None
    calendar_event: str | None
    sources: tuple[tuple[Path, str, str | None], ...]
