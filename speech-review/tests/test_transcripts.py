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


def transcript(
    path: Path,
    *,
    attendee_handle: str = "",
    first_speaker: str = "speaker-1",
) -> Path:
    attendees = (
        "attendees:\n"
        f"  - handle: {attendee_handle}\n"
        "    identity: ''\n"
        if attendee_handle
        else "attendees: []\n"
    )
    path.write_text(
        f"---\nsource_sha256: {'a' * 64}\nspeech2md_version: {'b' * 40}\n"
        + attendees
        + "---\n\n"
        f"# {path.stem}\n\n"
        "## Transcript\n\n"
        f"**[00:00:05.00] {first_speaker}:** Hello <!-- 3.25s --> there. <!-- 7.00s -->\n\n"
        "**[00:00:12.00] speaker-2:** General Kenobi. <!-- 10.00s -->\n"
    )
    return path


def test_discovers_and_parses_frozen_markdown(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md", attendee_handle="Alice")
    (tmp_path / "notes.md").write_text("# Notes\n")

    found = discover(tmp_path)
    assert [item.markdown for item in found] == [path]
    assert resolve_identifier(tmp_path, found[0].identifier).markdown == path
    parsed = parse_markdown(path)
    assert parsed["title"] == "meeting"
    assert parsed["turns"][0] == {
        "index": 0,
        "start": 5.0,
        "end": 12.0,
        "speaker": "speaker-1",
        "text": "Hello there.",
        "timestamps": [8.25, 12.0],
    }


def test_rejects_transcript_turn_without_an_explicit_end_marker(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md")
    path.write_text(path.read_text().replace(" <!-- 10.00s -->", ""))

    with pytest.raises(ValueError, match="missing timing comments"):
        parse_markdown(path)


def test_rejects_legacy_integer_turn_timestamps(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md")
    path.write_text(path.read_text().replace(".00]", "]"))

    with pytest.raises(ValueError, match="no supported timestamped turns"):
        parse_markdown(path)


def test_rejects_legacy_speaker_to_attendee_mapping(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md")
    path.write_text(path.read_text().replace(
        "attendees: []",
        "attendees:\n  - handle: speaker-1\n    identity: Alice",
    ))

    with pytest.raises(ValueError, match="unsupported attendee frontmatter"):
        parse_markdown(path)


def test_rejects_content_after_the_final_timing_marker(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md")
    path.write_text(path.read_text().replace(
        "General Kenobi. <!-- 10.00s -->",
        "General Kenobi. <!-- 10.00s --> trailing",
    ))

    with pytest.raises(ValueError, match="must end with a timing comment"):
        parse_markdown(path)


def test_turn_end_can_overlap_the_next_turn(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md")
    path.write_text(path.read_text().replace(
        "there. <!-- 7.00s -->",
        "there. <!-- 8.00s -->",
    ))

    parsed = parse_markdown(path)

    assert parsed["turns"][0]["end"] == 13.0
    assert parsed["turns"][1]["start"] == 12.0


def test_attendee_roster_does_not_identify_local_speakers(tmp_path: Path):
    parsed = parse_markdown(transcript(
        tmp_path / "meeting.md",
        attendee_handle="Alice",
    ))

    assert review_progress(parsed, {"attendees": [{"handle": "Alice", "identity": ""}]}) == {
        "complete": False,
        "unassignedRunCount": 2,
        "unassignedSpeakerCount": 2,
    }


def test_review_progress_counts_unnamed_slices(tmp_path: Path):
    parsed = parse_markdown(transcript(
        tmp_path / "meeting.md",
        attendee_handle="Alice",
        first_speaker="Alice",
    ))
    empty = review_progress(parsed, {"attendees": [{"handle": "Alice", "identity": ""}]})
    assert empty == {
        "complete": False,
        "unassignedRunCount": 1,
        "unassignedSpeakerCount": 1,
    }

    partial = review_progress(parsed, {
        "attendees": [{"handle": "Bob", "identity": "", "ranges": [{"start": 14, "end": 18}]}],
    })
    assert partial["unassignedRunCount"] == 2
    assert partial["unassignedSpeakerCount"] == 1

    complete = review_progress(parsed, {
        "attendees": [{"handle": "Bob", "identity": "", "ranges": [{"start": 12, "end": 22}]}],
    })
    assert complete == {
        "complete": True,
        "unassignedRunCount": 0,
        "unassignedSpeakerCount": 0,
    }


def test_review_progress_propagates_a_whole_turn_hint_to_its_handle(tmp_path: Path):
    parsed = parse_markdown(transcript(
        tmp_path / "meeting.md",
        attendee_handle="Alice",
        first_speaker="Alice",
    ))
    parsed["turns"].append({
        "index": 2,
        "start": 22,
        "end": 30,
        "speaker": "speaker-2",
        "text": "Another turn.",
    })

    progress = review_progress(parsed, {
        "attendees": [{"handle": "Bob", "identity": "", "ranges": [{"start": 12, "end": 22}]}],
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
        "attendees": [{
            "handle": "Michał",
            "identity": "",
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


def test_attendee_without_ranges_is_preserved_without_an_empty_ranges_field(tmp_path: Path):
    path = tmp_path / "meeting.hint.yaml"
    document = {
        "attendees": [{"handle": "Michał", "identity": "", "ranges": []}],
    }

    write_hint_document(path, document, None)

    assert path.read_text() == "attendees:\n- handle: Michał\n  identity: ''\n"


def test_separate_speakers_collection_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown fields"):
        write_hint_document(
            tmp_path / "meeting.hint.yaml",
            {"attendees": [], "speakers": []},
            None,
        )


def test_current_transcript_becomes_stale_when_hints_change(tmp_path: Path):
    path = transcript(tmp_path / "meeting.md", attendee_handle="Alice")
    item = TranscriptFile(tmp_path, path)
    assert item.status == "ready"

    write_hint_document(
        item.hint_path,
        {"hotwords": ["F2Z"], "attendees": [], "edits": []},
        None,
    )

    assert item.status == "stale"
    assert item.stale_reason == "hints"


def test_voiceprint_candidates_are_ranked_across_folder(tmp_path: Path):
    selected_path = transcript(tmp_path / "selected.md")
    alice_path = transcript(
        tmp_path / "alice.md", attendee_handle="Alice", first_speaker="Alice"
    )
    bob_path = transcript(
        tmp_path / "bob.md", attendee_handle="Bob", first_speaker="Bob"
    )
    np.savez(selected_path.with_suffix(".voiceprints.npz"), handles=np.array(["speaker-1"]), embeddings=np.array([[1.0, 0.0]]))
    np.savez(alice_path.with_suffix(".voiceprints.npz"), handles=np.array(["Alice"]), embeddings=np.array([[0.9, 0.1]]))
    np.savez(bob_path.with_suffix(".voiceprints.npz"), handles=np.array(["Bob"]), embeddings=np.array([[0.0, 1.0]]))

    candidates = candidate_identities(tmp_path, TranscriptFile(tmp_path, selected_path), "speaker-1")
    assert [item["identity"] for item in candidates] == ["Alice", "Bob"]
    assert candidates[0]["similarity"] > candidates[1]["similarity"]
