from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


@dataclass
class TranscriptState:
    schema_version: int
    source: str
    capture_manifest: str | None
    meeting_id: str | None
    title: str | None
    started_at: str | None
    model: str
    model_revision: str
    created_at: str
    processing_seconds: float
    audio: list[AudioSource]
    speakers: dict[str, str]
    segments: list[Segment]
    warnings: list[str]
    speaker_profiles: dict[str, SpeakerProfile] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    derived_artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TranscriptState:
        return cls(
            schema_version=value["schema_version"],
            source=value["source"],
            capture_manifest=value.get("capture_manifest"),
            meeting_id=value.get("meeting_id"),
            title=value.get("title"),
            started_at=value.get("started_at"),
            model=value["model"],
            model_revision=value["model_revision"],
            created_at=value["created_at"],
            processing_seconds=value.get("processing_seconds", 0),
            audio=[AudioSource(**item) for item in value["audio"]],
            speakers=dict(value.get("speakers", {})),
            segments=[Segment(**item) for item in value["segments"]],
            warnings=list(value.get("warnings", [])),
            speaker_profiles={
                speaker: SpeakerProfile(
                    speaker=item.get("speaker", speaker),
                    model=item["model"],
                    model_revision=item["model_revision"],
                    checkpoint_sha256=item["checkpoint_sha256"],
                    embedding_dimension=item["embedding_dimension"],
                    samples=[EmbeddingSample(**sample) for sample in item.get("samples", [])],
                )
                for speaker, item in value.get("speaker_profiles", {}).items()
            },
            provenance=dict(value.get("provenance", {})),
            derived_artifacts=list(value.get("derived_artifacts", [])),
        )


@dataclass(frozen=True)
class ResolvedInput:
    requested: Path
    state_path: Path
    markdown_path: Path
    capture_manifest: Path | None
    meeting_id: str | None
    title: str | None
    started_at: str | None
    sources: tuple[tuple[Path, str, str | None], ...]
