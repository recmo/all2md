from __future__ import annotations

from pathlib import Path
import json

import pytest

from audio2md.media import resolve_input
from audio2md.model import AudioSource, EmbeddingSample, Segment, SpeakerProfile, TranscriptState
from audio2md.moss import (
    MOSS_MODEL,
    MOSS_REVISION,
    deduplicate_boundaries,
    parse_silence_centers,
    parse_segments,
    plan_windows,
    trim_overlaps,
)
from audio2md.pipeline import relabel, transcribe, write_state
from audio2md.render import coalesce_segments, render_markdown, timestamp


def test_resolve_media_input(tmp_path: Path):
    source = tmp_path / "meeting.mp4"
    source.touch()
    resolved = resolve_input(source)
    assert resolved.sources == ((source, "mixed", None),)
    assert resolved.state_path.name == "meeting.audio2md.json"
    assert resolved.markdown_path.name == "meeting.md"


def test_resolve_capture_manifest(tmp_path: Path):
    (tmp_path / "meeting-microphone.flac").touch()
    manifest = tmp_path / "2026-08-02-meeting-capture.json"
    manifest.write_text(json.dumps({
        "schemaVersion": 1,
        "meetingID": "id",
        "startedAt": "now",
        "status": "complete",
        "audio": [{"file": "meeting-microphone.flac", "role": "microphone", "sha256": "a" * 64}],
    }))
    resolved = resolve_input(manifest)
    assert resolved.meeting_id == "id"
    assert resolved.sources[0][1] == "microphone"
    assert resolved.state_path.name == "2026-08-02-meeting.audio2md.json"
    assert resolved.markdown_path.name == "2026-08-02-meeting.md"


def test_window_plan_covers_long_recording():
    assert plan_windows(1200, silence_centers=[]) == [(0, 1200)]
    assert plan_windows(4582, silence_centers=[1520, 3060]) == [
        (0, 1521),
        (1519, 3061),
        (3059, 4582),
    ]
    with pytest.raises(RuntimeError, match="no silence found"):
        plan_windows(4582, silence_centers=[])


def test_parse_silence_centers():
    log = """
silence_start: 10.5
silence_end: 12.5 | silence_duration: 2
silence_start: 20
silence_end: 21 | silence_duration: 1
"""
    assert parse_silence_centers(log) == [11.5, 20.5]


def test_parse_moss_native_speakers_and_microphone_identity():
    raw = [{"start": 1, "end": 2, "speaker_id": "S03", "text": "[S03] Hello"}]
    participants = parse_segments(raw, window=2, offset=10, role="participants")
    microphone = parse_segments(raw, window=2, offset=10, role="microphone")
    assert participants == [Segment(11, 12, "Hello", "W02:S03", "participants")]
    assert microphone[0].speaker == "Remco"


def test_trim_and_deduplicate_chunk_boundary():
    windows = [(0, 300), (295, 595)]
    first = [Segment(292, 299.8, "specifically look into 32 bit limbs", "Speaker 1", "mixed")]
    second = [
        Segment(295.3, 300.1, "look into 32 bit limbs", "Speaker 1", "mixed"),
        Segment(301, 303, "next thought", "Speaker 1", "mixed"),
    ]
    selected = trim_overlaps([first, second], windows)
    assert len(deduplicate_boundaries(selected)) == 2


def test_render_and_relabel_without_retranscription(tmp_path: Path):
    media = tmp_path / "meeting.mp4"
    media.touch()
    state = fixture_state(media)
    write_state(state, tmp_path / "meeting.audio2md.json")
    assert "**[00:00:01] Speaker 1:** Hello" in render_markdown(state)
    relabeled = relabel(media, ["Speaker 1=Alice"])
    assert relabeled.speakers == {"Speaker 1": "Alice"}
    assert "Alice" in (tmp_path / "meeting.md").read_text()


def test_render_coalesces_only_same_speaker():
    segments = [
        Segment(0, 1, "One.", "Speaker 1", "mixed"),
        Segment(1.5, 2, "Two.", "Speaker 1", "mixed"),
        Segment(2.1, 3, "Three.", "Speaker 2", "mixed"),
    ]
    assert [item.text for item in coalesce_segments(segments)] == ["One. Two.", "Three."]
    assert timestamp(3661) == "01:01:01"


def test_transcribe_refuses_to_overwrite_before_loading_model(tmp_path: Path):
    media = tmp_path / "meeting.mp4"
    media.touch()
    (tmp_path / "meeting.audio2md.json").write_text("existing")
    with pytest.raises(FileExistsError, match="--force"):
        transcribe(media)


def test_state_round_trip_with_embeddings_and_backward_compatible_loading(tmp_path: Path):
    state = fixture_state(tmp_path / "meeting.mp4")
    sample = EmbeddingSample(
        vector=[1.0] + [0.0] * 191,
        source_track="participants",
        start=1,
        end=4,
        duration_seconds=3,
        window=1,
        quality={"overlap_free": True},
    )
    state.schema_version = 2
    state.speaker_profiles = {
        "Speaker 1": SpeakerProfile(
            speaker="Speaker 1",
            model="ReDimNet2",
            model_revision="revision",
            checkpoint_sha256="b" * 64,
            embedding_dimension=192,
            samples=[sample],
        )
    }
    restored = TranscriptState.from_dict(state.to_dict())
    assert restored.speaker_profiles["Speaker 1"].samples[0] == sample

    old_value = state.to_dict()
    old_value["schema_version"] = 1
    old_value.pop("speaker_profiles")
    assert TranscriptState.from_dict(old_value).speaker_profiles == {}


def fixture_state(media: Path) -> TranscriptState:
    return TranscriptState(
        schema_version=1,
        source=str(media),
        capture_manifest=None,
        meeting_id=None,
        title="Test",
        started_at=None,
        model=MOSS_MODEL,
        model_revision=MOSS_REVISION,
        created_at="now",
        processing_seconds=1,
        audio=[AudioSource(str(media), "mixed", "a" * 64, 3, "aac")],
        speakers={},
        segments=[Segment(1, 2, "Hello", "Speaker 1", "mixed")],
        warnings=[],
    )
