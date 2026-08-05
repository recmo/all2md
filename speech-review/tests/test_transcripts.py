from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from speech_review.transcripts import (
    TranscriptFile,
    candidate_identities,
    discover,
    load_hint_document,
    parse_markdown,
    review_progress,
    resolve_identifier,
    write_hint_document,
)


def transcript(path: Path, *, identity: str = "", handle: str = "speaker-1") -> Path:
    attendee = f"  - handle: {handle}\n    identity: {identity}\n" if handle else f"  - identity: {identity}\n"
    path.write_text(
        "---\n"
        "source_sha256: " + "a" * 64 + "\n"
        "speech2md_version: " + "b" * 40 + "\n"
        "attendees:\n" + attendee +
        "---\n\n"
        f"# {path.stem}\n\n"
        "## Transcript\n\n"
        "**[00:00:05] speaker-1:** Hello there.\n\n"
        "**[00:00:12] speaker-2:** General Kenobi.\n"
    )
    return path


def test_discovers_and_parses_frozen_markdown(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md", identity="Alice")
    (tmp_path / "notes.md").write_text("# Notes\n")

    found = discover(tmp_path)
    assert [item.markdown for item in found] == [path]
    assert resolve_identifier(tmp_path, found[0].identifier).markdown == path
    parsed = parse_markdown(path)
    assert parsed["title"] == "meeting"
    assert parsed["turns"][0] == {
        "index": 0,
        "start": 5,
        "end": 12,
        "speaker": "speaker-1",
        "text": "Hello there.",
    }


def test_review_progress_counts_unnamed_slices(tmp_path: Path):
    parsed = parse_markdown(transcript(tmp_path / "meeting.md", identity="Alice"))
    empty = review_progress(parsed, {"speakers": []})
    assert empty == {
        "complete": False,
        "unassignedRunCount": 1,
        "unassignedSpeakerCount": 1,
    }

    partial = review_progress(parsed, {
        "speakers": [{"identity": "Bob", "ranges": [{"start": 14, "end": 18}]}],
    })
    assert partial["unassignedRunCount"] == 2
    assert partial["unassignedSpeakerCount"] == 1

    complete = review_progress(parsed, {
        "speakers": [{"identity": "Bob", "ranges": [{"start": 12, "end": 22}]}],
    })
    assert complete == {
        "complete": True,
        "unassignedRunCount": 0,
        "unassignedSpeakerCount": 0,
    }


def test_review_progress_propagates_a_whole_turn_hint_to_its_handle(tmp_path: Path):
    parsed = parse_markdown(transcript(tmp_path / "meeting.md", identity="Alice"))
    parsed["turns"].append({
        "index": 2,
        "start": 22,
        "end": 30,
        "speaker": "speaker-2",
        "text": "Another turn.",
    })

    progress = review_progress(parsed, {
        "speakers": [{"identity": "Bob", "ranges": [{"start": 12, "end": 22}]}],
    })

    assert progress == {
        "complete": True,
        "unassignedRunCount": 0,
        "unassignedSpeakerCount": 0,
    }


def test_lists_unprocessed_and_stale_recordings(tmp_path: Path):
    unprocessed = tmp_path / "unprocessed.mp4"
    unprocessed.touch()
    stale = tmp_path / "stale.mp4"
    stale.touch()
    stale.with_suffix(".md").write_text(
        "---\ntitle: Earlier transcript\nspeech2md:\n  schema_version: 2\n---\n"
    )

    found = {item.requested.name: item for item in discover(tmp_path)}
    assert found["unprocessed.mp4"].status == "unprocessed"
    assert found["stale.mp4"].status == "stale"


def test_hint_writes_are_atomic_and_revision_checked(tmp_path: Path):
    path = tmp_path / "meeting.hint.yaml"
    document = {
        "hotwords": ["F2Z"],
        "title": "ProveKit weekly check-in",
        "started_at": "2026-08-04T09:00:00+02:00",
        "ended_at": "2026-08-04T10:00:00+02:00",
        "calendar_event": "https://calendar.google.com/example",
        "attendees": ["Michał"],
        "speakers": [{
            "identity": "Alice",
            "ranges": [{"track": "participants", "start": 5, "end": 12}],
        }],
        "edits": [{
            "track": "participants",
            "start": 5,
            "end": 12,
            "before": "F two Z",
            "after": "F2Z",
        }],
    }
    revision = write_hint_document(path, document, None)
    loaded, loaded_revision = load_hint_document(path)
    assert loaded == document
    assert loaded_revision == revision
    with pytest.raises(RuntimeError, match="changed on disk"):
        write_hint_document(path, document, None)


def test_current_transcript_becomes_stale_when_hints_change(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md", identity="Alice")
    item = TranscriptFile(tmp_path, path)
    assert item.status == "ready"

    write_hint_document(
        item.hint_path,
        {"hotwords": ["F2Z"], "attendees": [], "speakers": [], "edits": []},
        None,
    )

    assert item.status == "stale"
    assert item.stale_reason == "hints"


def test_voiceprint_candidates_are_ranked_across_folder(tmp_path: Path):
    selected_path = transcript(tmp_path / "selected.md")
    alice_path = transcript(tmp_path / "alice.md", identity="Alice")
    bob_path = transcript(tmp_path / "bob.md", identity="Bob")
    np.savez(selected_path.with_suffix(".voiceprints.npz"), handles=np.array(["speaker-1"]), embeddings=np.array([[1.0, 0.0]]))
    np.savez(alice_path.with_suffix(".voiceprints.npz"), handles=np.array(["speaker-1"]), embeddings=np.array([[0.9, 0.1]]))
    np.savez(bob_path.with_suffix(".voiceprints.npz"), handles=np.array(["speaker-1"]), embeddings=np.array([[0.0, 1.0]]))

    candidates = candidate_identities(tmp_path, TranscriptFile(tmp_path, selected_path), "speaker-1")
    assert [item["identity"] for item in candidates] == ["Alice", "Bob"]
    assert candidates[0]["similarity"] > candidates[1]["similarity"]
