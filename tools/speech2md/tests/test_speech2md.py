from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace

import pytest
import yaml

import speech2md.moss as moss
import speech2md.pipeline as pipeline
from speech2md.benchmark import summarize
from speech2md.cli import parser
from speech2md.media import resolve_input
from speech2md.model import AudioSource, EmbeddingSample, Segment, SpeakerProfile, TranscriptState
from speech2md.moss import (
    ENGLISH_TRANSCRIPTION_PROMPT,
    MAX_HOTWORDS,
    MOSS_MODEL,
    MOSS_REVISION,
    MAX_GENERATION_TOKENS,
    RECOVERY_TOKEN_THRESHOLD,
    build_transcription_prompt,
    deduplicate_boundaries,
    generation_diagnostics,
    _generate_with_timestamp_progress,
    parse_moss_transcript,
    parse_silence_centers,
    parse_segments,
    plan_windows,
    transcribe_track,
    trim_overlaps,
)
from speech2md.pipeline import relabel, transcribe, write_state
from speech2md.render import coalesce_segments, render_markdown, timestamp


def test_resolve_media_input(tmp_path: Path):
    source = tmp_path / "meeting.mp4"
    source.touch()
    resolved = resolve_input(source)
    assert resolved.sources == ((source, "mixed", None),)
    assert resolved.state_path.name == "meeting.speech2md.json"
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
    assert resolved.state_path.name == "2026-08-02-meeting.speech2md.json"
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
    participants = parse_segments(
        raw, window=2, offset=10, duration=10, role="participants"
    )
    microphone = parse_segments(
        raw, window=2, offset=10, duration=10, role="microphone"
    )
    assert participants == [Segment(11, 12, "Hello", "W02:S03", "participants")]
    assert microphone[0].speaker == "Remco"
    with pytest.raises(ValueError, match="missing MOSS speaker id"):
        parse_segments(
            [{"start": 1, "end": 2, "text": "Hello"}],
            window=1,
            offset=0,
            duration=10,
            role="mixed",
        )
    with pytest.raises(ValueError, match="outside the submitted"):
        parse_segments(
            [{"start": 1, "end": 11, "speaker_id": "S01", "text": "Hello"}],
            window=1,
            offset=0,
            duration=10,
            role="mixed",
        )


def test_parse_moss_transcript_preserves_numeric_brackets_in_text():
    raw = "[0.0][S01]Version [123] is here[3.0][3.1][S02]Next[4.0]"
    assert parse_moss_transcript(raw) == [
        {"start": 0.0, "end": 3.0, "speaker_id": "S01", "text": "Version [123] is here"},
        {"start": 3.1, "end": 4.0, "speaker_id": "S02", "text": "Next"},
    ]


def test_parse_moss_transcript_does_not_invent_fallback_segment():
    assert parse_moss_transcript("plain unstructured model output") == []
    assert parse_moss_transcript("[0][S01]incomplete[1]trailing text") == []
    assert MAX_GENERATION_TOKENS == 16_384


def test_generation_diagnostics_marks_only_a_hard_ceiling_hit():
    valid = "[0.0][S01]Hello[1.0]"
    below = generation_diagnostics(SimpleNamespace(
        text=valid,
        generation_tokens=MAX_GENERATION_TOKENS - 1,
    ))
    at_limit = generation_diagnostics(SimpleNamespace(
        text=valid,
        generation_tokens=MAX_GENERATION_TOKENS,
    ))
    invalid = generation_diagnostics(SimpleNamespace(
        text="plain unstructured model output",
        generation_tokens=12,
    ))
    assert below["possibly_truncated"] is False
    assert at_limit["possibly_truncated"] is True
    assert invalid["parse_status"] == "invalid"


def test_build_transcription_prompt_uses_english_and_targeted_hotwords():
    assert build_transcription_prompt() == ENGLISH_TRANSCRIPTION_PROMPT
    assert build_transcription_prompt([" Remco ", "F2Z", "remco"]) == (
        f"{ENGLISH_TRANSCRIPTION_PROMPT} Hotwords: Remco, F2Z"
    )
    with pytest.raises(ValueError, match="single-line"):
        build_transcription_prompt(["bad\nword"])
    with pytest.raises(ValueError, match=f"at most {MAX_HOTWORDS}"):
        build_transcription_prompt([f"term-{index}" for index in range(MAX_HOTWORDS + 1)])


def test_cli_accepts_comma_separated_hotwords():
    arguments = parser().parse_args([
        "transcribe", "meeting.mp4", "--hotwords", "Remco, Piotr,F2Z",
    ])
    assert arguments.hotwords == "Remco, Piotr,F2Z"


@pytest.mark.parametrize(
    "text, expected_texts",
    [
        (
            "[0][S01]Complete[1][2][S02]incomplete",
            ["Complete"],
        ),
        (
            "[0][S01]Complete[1][2][S02]malformed tail[3][S03]Later[4]",
            ["Complete", "Later"],
        ),
    ],
)
def test_generation_diagnostics_marks_partially_parsed_output(text, expected_texts):
    diagnostics = generation_diagnostics(SimpleNamespace(
        text=text,
        generation_tokens=20,
    ))
    assert [segment["text"] for segment in diagnostics["parsed"]] == expected_texts
    assert diagnostics["possibly_truncated"] is False
    assert diagnostics["parse_status"] == "partial"


def test_trim_and_deduplicate_chunk_boundary():
    windows = [(0, 300), (295, 595)]
    first = [Segment(292, 299.8, "specifically look into 32 bit limbs", "Speaker 1", "mixed")]
    second = [
        Segment(295.3, 300.1, "look into 32 bit limbs", "Speaker 1", "mixed"),
        Segment(301, 303, "next thought", "Speaker 1", "mixed"),
    ]
    selected = trim_overlaps([first, second], windows)
    assert len(deduplicate_boundaries(selected)) == 2


def test_streaming_generation_reports_output_timestamps():
    updates = [
        SimpleNamespace(text="[0][S01]Hello", generation_tokens=3),
        SimpleNamespace(text=" there[12.5]", generation_tokens=5),
        SimpleNamespace(text="[13][S02]Next[20]", generation_tokens=9),
        SimpleNamespace(text="", generation_tokens=9),
    ]
    engine = SimpleNamespace(generate=lambda *args, **kwargs: iter(updates))
    timestamps = []

    result = _generate_with_timestamp_progress(
        engine,
        "window.wav",
        prompt=ENGLISH_TRANSCRIPTION_PROMPT,
        timestamp_callback=timestamps.append,
    )

    assert result.text == "[0][S01]Hello there[12.5][13][S02]Next[20]"
    assert result.generation_tokens == 9
    assert timestamps == [0.0, 12.5, 13.0, 20.0]


def test_transcribe_recovers_when_token_count_is_suspect(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "meeting.wav"
    source.touch()
    ffmpeg_calls = []
    results = iter([
        SimpleNamespace(
            text="[0][S01]First pass[100]",
            prompt_tokens=10,
            generation_tokens=RECOVERY_TOKEN_THRESHOLD,
            total_tokens=RECOVERY_TOKEN_THRESHOLD + 10,
        ),
        SimpleNamespace(
            text="[0][S01]Overlap verification[40][41][S01]Rest[230]",
            prompt_tokens=10,
            generation_tokens=200,
            total_tokens=210,
        ),
    ])
    engine = SimpleNamespace(generate=lambda *args, **kwargs: next(results))
    progress = []

    monkeypatch.setattr(
        moss.subprocess,
        "run",
        lambda command, **kwargs: ffmpeg_calls.append(command) or SimpleNamespace(),
    )

    segments, raw, _ = transcribe_track(
        source,
        engine=engine,
        prompt=ENGLISH_TRANSCRIPTION_PROMPT,
        role="microphone",
        duration=300,
        progress_callback=lambda *event: progress.append(event),
    )

    assert [(item.start, item.end) for item in segments] == [
        (0, 100),
        (70, 110),
        (111, 300),
    ]
    assert len(raw["windows"]) == 2
    assert raw["windows"][0]["coverage_end"] == 100
    assert raw["windows"][0]["requires_recovery"] is True
    assert raw["windows"][1]["source_start"] == 70
    assert raw["windows"][1]["attempt"] == 2
    assert raw["windows"][1]["requires_recovery"] is False
    assert ffmpeg_calls[1][ffmpeg_calls[1].index("-ss") + 1] == "70.0"
    assert ffmpeg_calls[1][ffmpeg_calls[1].index("-t") + 1] == "230.0"
    assert raw["actual_overlap_seconds"] == 30
    assert progress == [
        (1, 1, 1, 0.0),
        (1, 1, 2, 0.0),
        (1, 1, 2, 300.0),
    ]


def test_transcribe_accepts_trailing_silence_when_token_count_is_safe(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "meeting.wav"
    source.touch()
    calls = []
    engine = SimpleNamespace(generate=lambda *args, **kwargs: (
        calls.append(True)
        or SimpleNamespace(text="[0][S01]Last speech[250]", generation_tokens=100)
    ))
    monkeypatch.setattr(
        moss.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    segments, raw, _ = transcribe_track(
        source,
        engine=engine,
        prompt=ENGLISH_TRANSCRIPTION_PROMPT,
        role="microphone",
        duration=300,
    )

    assert calls == [True]
    assert [(item.start, item.end) for item in segments] == [(0, 250)]
    assert raw["windows"][0]["coverage_gap_seconds"] == 50
    assert raw["windows"][0]["requires_recovery"] is False


def test_transcribe_fails_when_recovery_makes_no_coverage_progress(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "meeting.wav"
    source.touch()
    results = iter([
        SimpleNamespace(
            text="[0][S01]First pass[100]",
            generation_tokens=RECOVERY_TOKEN_THRESHOLD,
        ),
        SimpleNamespace(
            text="[0][S01]Same endpoint[30]",
            generation_tokens=RECOVERY_TOKEN_THRESHOLD,
        ),
    ])
    engine = SimpleNamespace(generate=lambda *args, **kwargs: next(results))
    monkeypatch.setattr(
        moss.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="no meaningful coverage progress"):
        transcribe_track(
            source,
            engine=engine,
            prompt=ENGLISH_TRANSCRIPTION_PROMPT,
            role="microphone",
            duration=300,
        )


def test_transcribe_verifies_high_token_output_even_when_timestamp_is_near_end(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "meeting.wav"
    source.touch()
    results = iter([
        SimpleNamespace(
            text="[0][S01]Long pass[290]",
            generation_tokens=RECOVERY_TOKEN_THRESHOLD,
        ),
        SimpleNamespace(text="[0][S01]Verified tail[40]", generation_tokens=100),
    ])
    engine = SimpleNamespace(generate=lambda *args, **kwargs: next(results))
    monkeypatch.setattr(
        moss.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    segments, raw, _ = transcribe_track(
        source,
        engine=engine,
        prompt=ENGLISH_TRANSCRIPTION_PROMPT,
        role="microphone",
        duration=300,
    )

    assert raw["windows"][0]["coverage_gap_seconds"] == 10
    assert raw["windows"][0]["token_count_suspect"] is True
    assert raw["windows"][0]["requires_recovery"] is True
    assert raw["windows"][1]["source_start"] == 260
    assert raw["windows"][1]["token_count_suspect"] is False
    assert raw["windows"][1]["requires_recovery"] is False
    assert segments[-1].end == 300


def test_transcribe_rejects_timestamp_outside_submitted_span(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "meeting.wav"
    source.touch()
    engine = SimpleNamespace(generate=lambda *args, **kwargs: SimpleNamespace(
        text="[0][S01]Bad timestamp[999]",
        generation_tokens=100,
    ))
    monkeypatch.setattr(
        moss.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="outside the submitted 300.00s audio span"):
        transcribe_track(
            source,
            engine=engine,
            prompt=ENGLISH_TRANSCRIPTION_PROMPT,
            role="microphone",
            duration=300,
        )


def test_render_and_relabel_without_retranscription(tmp_path: Path):
    media = tmp_path / "meeting.mp4"
    media.touch()
    state = fixture_state(media)
    write_state(state, tmp_path / "meeting.speech2md.json")
    assert "**[00:00:01] Speaker 1:** Hello" in render_markdown(state)
    relabeled = relabel(media, ["Speaker 1=Alice"])
    assert relabeled.speakers == {"Speaker 1": "Alice"}
    assert "Alice" in (tmp_path / "meeting.md").read_text()


def test_render_moves_mechanical_details_to_frontmatter(tmp_path: Path):
    state = fixture_state(tmp_path / "meeting.mp4")
    state.warnings = ["manual review recommended"]
    state.derived_artifacts = ["meeting.moss.json"]

    rendered = render_markdown(state)
    opening, raw_frontmatter, body = rendered.split("---", 2)
    metadata = yaml.safe_load(raw_frontmatter)

    assert opening == ""
    assert metadata == {
        "title": "Test",
        "speech2md": {
            "schema_version": 1,
            "source": str(tmp_path / "meeting.mp4"),
            "capture_manifest": None,
            "meeting_id": None,
            "started_at": None,
            "created_at": "now",
            "model": {"id": MOSS_MODEL, "revision": MOSS_REVISION},
            "audio": [{
                "path": str(tmp_path / "meeting.mp4"),
                "role": "mixed",
                "sha256": "a" * 64,
                "duration_seconds": 3,
                "format": "aac",
            }],
            "processing": {
                "duration_seconds": 1,
                "warnings": ["manual review recommended"],
            },
            "derived_artifacts": ["meeting.moss.json"],
        },
    }
    assert body.startswith("\n\n# Test\n\n## Transcript\n")
    assert "## Capture" not in body
    assert "## Processing notes" not in body


@pytest.mark.parametrize("suffix", [".audio2md.json", ".voice2md.json"])
def test_relabel_reads_legacy_state(tmp_path: Path, suffix: str):
    media = tmp_path / "meeting.mp4"
    media.touch()
    legacy_state = tmp_path / f"meeting{suffix}"
    write_state(fixture_state(media), legacy_state)

    relabeled = relabel(media, ["Speaker 1=Alice"])

    assert relabeled.speakers == {"Speaker 1": "Alice"}
    assert json.loads(legacy_state.read_text())["speakers"] == {"Speaker 1": "Alice"}
    assert not (tmp_path / "meeting.speech2md.json").exists()


def test_benchmark_reads_legacy_state_without_double_counting(tmp_path: Path):
    media = tmp_path / "meeting.mp4"
    state = fixture_state(media)
    write_state(state, tmp_path / "meeting.audio2md.json")
    assert summarize(tmp_path)["recordings"] == 1

    write_state(state, tmp_path / "meeting.voice2md.json")
    assert summarize(tmp_path)["recordings"] == 1

    write_state(state, tmp_path / "meeting.speech2md.json")
    assert summarize(tmp_path)["recordings"] == 1


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
    (tmp_path / "meeting.speech2md.json").write_text("existing")
    with pytest.raises(FileExistsError, match="--force"):
        transcribe(media)


def test_transcribe_loads_one_moss_engine_for_all_tracks(tmp_path: Path, monkeypatch):
    requested = tmp_path / "capture.json"
    microphone = tmp_path / "microphone.flac"
    participants = tmp_path / "participants.flac"
    microphone.touch()
    participants.touch()
    resolved = SimpleNamespace(
        requested=requested,
        state_path=tmp_path / "capture.speech2md.json",
        markdown_path=tmp_path / "capture.md",
        capture_manifest=requested,
        meeting_id="meeting",
        title="Meeting",
        started_at=None,
        sources=(
            (microphone, "microphone", None),
            (participants, "participants", None),
        ),
    )
    engine = object()
    load_calls = []
    seen_engines = []
    seen_prompts = []
    progress_bars = []

    class RecordingProgress:
        def __init__(self, **kwargs):
            self.total = kwargs["total"]
            self.completed = 0.0
            self.postfixes = []
            progress_bars.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def set_postfix_str(self, value):
            self.postfixes.append(value)

        def update(self, amount):
            self.completed += amount

    monkeypatch.setattr(pipeline, "resolve_input", lambda _: resolved)
    monkeypatch.setattr(
        pipeline,
        "probe",
        lambda path, *, expected_sha256, role: AudioSource(
            str(path), role, "a" * 64, 10.0, "flac"
        ),
    )
    monkeypatch.setattr(pipeline, "get_redimnet2_embedder", object)
    monkeypatch.setattr(pipeline, "tqdm", RecordingProgress)
    monkeypatch.setattr(
        pipeline,
        "load_moss_engine",
        lambda: load_calls.append(True) or engine,
    )

    def fake_transcribe_track(path, *, engine, prompt, speaker_profiles, **kwargs):
        seen_engines.append(engine)
        seen_prompts.append(prompt)
        kwargs["progress_callback"](1, 1, 1, kwargs["duration"])
        return [], {"windows": [{}], "actual_overlap_seconds": 0.0, "warnings": []}, speaker_profiles

    monkeypatch.setattr(pipeline, "transcribe_track", fake_transcribe_track)
    monkeypatch.setattr(pipeline, "write_json", lambda *args: None)
    monkeypatch.setattr(pipeline, "write_state", lambda *args: None)
    monkeypatch.setattr(pipeline, "write_text", lambda *args: None)

    pipeline.transcribe(requested, hotwords=["ProveKit", "F2Z"])

    assert load_calls == [True]
    assert seen_engines == [engine, engine]
    assert seen_prompts == [
        f"{ENGLISH_TRANSCRIPTION_PROMPT} Hotwords: ProveKit, F2Z",
        f"{ENGLISH_TRANSCRIPTION_PROMPT} Hotwords: ProveKit, F2Z",
    ]
    assert len(progress_bars) == 1
    assert progress_bars[0].total == 20.0
    assert progress_bars[0].completed == 20.0
    assert "participants window 1/1" in progress_bars[0].postfixes


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
