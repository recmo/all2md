from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from speech2md.hints import SpeechHints, load_hints, validate_hints
from speech2md.model import AudioSource, SpeakerHint


def source(role: str = "mixed", duration: float = 900) -> AudioSource:
    return AudioSource(f"/tmp/{role}.flac", role, "a" * 64, duration, "flac")


def test_absent_and_empty_hint_sidecars(tmp_path: Path):
    absent = tmp_path / "absent.hint.yaml"
    assert load_hints(absent) == SpeechHints()

    empty = tmp_path / "empty.hint.yaml"
    empty.write_text("")
    hints = load_hints(empty)
    assert hints.hotwords == ()
    assert hints.speakers == ()
    assert hints.sha256 == hashlib.sha256(b"").hexdigest()


def test_combined_hints_are_strictly_parsed_and_hotwords_normalized(tmp_path: Path):
    path = tmp_path / "meeting.hint.yaml"
    raw = (
        "hotwords:\n"
        "  - ' ProveKit '\n"
        "  - F2Z\n"
        "  - provekit\n"
        "speakers:\n"
        "  - identity: gbrain://people/alice\n"
        "    ranges:\n"
        "      - track: participants\n"
        "        start: 12.5\n"
        "        end: 18\n"
    )
    path.write_text(raw)
    hints = load_hints(path)
    assert hints.hotwords == ("ProveKit", "F2Z")
    assert hints.speakers == (
        SpeakerHint("gbrain://people/alice", 12.5, 18.0, "participants"),
    )
    assert hints.sha256 == hashlib.sha256(raw.encode()).hexdigest()


@pytest.mark.parametrize(
    "raw, message",
    [
        ("unknown: true\n", "unknown field"),
        ("hotwords: F2Z\n", "must be a list"),
        ("hotwords: [F2Z, 3]\n", "only strings"),
        ("hotwords: ['']\n", "must not be empty"),
        ("speakers: {}\n", "must be a list"),
        (
            "speakers:\n  - identity: ''\n    ranges: [{start: 1, end: 2}]\n",
            "identity must be a non-empty string",
        ),
        (
            "speakers:\n  - identity: Alice\n    ranges: [{start: 2, end: 1}]\n",
            "0 <= start < end",
        ),
        (
            "speakers:\n  - identity: Alice\n    extra: true\n    ranges: [{start: 1, end: 2}]\n",
            "unknown field",
        ),
        (
            "speakers:\n  - identity: Alice\n    ranges: [{start: 1, end: 2, extra: true}]\n",
            "unknown field",
        ),
    ],
)
def test_invalid_hint_shapes_fail_clearly(tmp_path: Path, raw: str, message: str):
    path = tmp_path / "meeting.hint.yaml"
    path.write_text(raw)
    with pytest.raises(ValueError, match=message):
        load_hints(path)


def test_malformed_yaml_fails_clearly(tmp_path: Path):
    path = tmp_path / "meeting.hint.yaml"
    path.write_text("speakers: [\n")
    with pytest.raises(ValueError, match="invalid hint YAML"):
        load_hints(path)


def test_single_track_is_inferred_and_ranges_are_checked():
    hints = SpeechHints(
        speakers=(SpeakerHint("Alice", 10, 20),),
        sha256="b" * 64,
    )
    validated = validate_hints(hints, [source()])
    assert validated.speakers == (SpeakerHint("Alice", 10, 20, "mixed"),)
    assert validated.sha256 == "b" * 64

    with pytest.raises(ValueError, match="exceeds mixed duration"):
        validate_hints(
            SpeechHints(speakers=(SpeakerHint("Alice", 899, 901),)),
            [source()],
        )


def test_multitrack_hints_require_a_unique_known_track():
    sources = [source("microphone"), source("participants")]
    with pytest.raises(ValueError, match="requires a track"):
        validate_hints(
            SpeechHints(speakers=(SpeakerHint("Alice", 10, 20),)),
            sources,
        )
    with pytest.raises(ValueError, match="unknown hint track"):
        validate_hints(
            SpeechHints(speakers=(SpeakerHint("Alice", 10, 20, "mixed"),)),
            sources,
        )


def test_conflicting_identity_ranges_are_rejected():
    hints = SpeechHints(speakers=(
        SpeakerHint("Alice", 10, 20, "participants"),
        SpeakerHint("Bob", 19, 30, "participants"),
    ))
    with pytest.raises(ValueError, match="conflicting hint ranges"):
        validate_hints(hints, [source("participants")])

    same_identity = SpeechHints(speakers=(
        SpeakerHint("Alice", 10, 20, "participants"),
        SpeakerHint("Alice", 19, 30, "participants"),
    ))
    assert len(validate_hints(same_identity, [source("participants")]).speakers) == 2
