from __future__ import annotations

import math
from pathlib import Path

import pytest
import numpy as np

from speech2md.model import EmbeddingSample, Segment, SpeakerProfile
from speech2md.redimnet2 import (
    REDIMNET2_DIMENSION,
    aggregate_voiceprints,
    extract_window_evidence,
    normalize_embedding,
    reconcile_speakers,
    select_clean_intervals,
    write_voiceprints,
)


class FakeEmbedder:
    def __init__(self, vectors: dict[tuple[float, float], list[float]] | None = None):
        self.vectors = vectors or {}
        self.calls: list[tuple[Path, float, float]] = []

    def embed(self, path: Path, start: float, end: float) -> list[float]:
        self.calls.append((path, start, end))
        return self.vectors.get((start, end), axis(0))


def axis(index: int) -> list[float]:
    vector = [0.0] * REDIMNET2_DIMENSION
    vector[index] = 1.0
    return vector


def angled(cosine: float) -> list[float]:
    vector = [0.0] * REDIMNET2_DIMENSION
    vector[0] = cosine
    vector[1] = math.sqrt(1 - cosine * cosine)
    return vector


def sample(vector: list[float], *, window: int = 1, start: float = 0) -> EmbeddingSample:
    return EmbeddingSample(
        vector=normalize_embedding(vector),
        source_track="participants",
        start=start,
        end=start + 3,
        duration_seconds=3,
        window=window,
        quality={"overlap_free": True},
    )


def evidence(**speakers: list[float]) -> dict[str, list[EmbeddingSample]]:
    return {speaker: [sample(vector)] for speaker, vector in speakers.items()}


def test_selects_clean_distributed_intervals_and_crops_long_speech():
    segments = [
        Segment(index * 10, index * 10 + 8, str(index), "W01:S01", "participants")
        for index in range(6)
    ]
    selected = select_clean_intervals(segments, window=1)
    assert len(selected["W01:S01"]) == 4
    assert selected["W01:S01"][0].start == 0
    assert selected["W01:S01"][-1].end == 58
    assert all(2 <= interval.duration_seconds <= 6 for interval in selected["W01:S01"])


def test_rejects_short_and_overlapping_speech():
    segments = [
        Segment(0, 1.9, "short", "W01:S01", "participants"),
        Segment(3, 7, "first", "W01:S01", "participants"),
        Segment(6, 8, "overlap", "W01:S02", "participants"),
        Segment(10, 13, "clean", "W01:S02", "participants"),
    ]
    selected = select_clean_intervals(segments, window=1)
    assert "W01:S01" not in selected
    assert [(item.start, item.end) for item in selected["W01:S02"]] == [(10, 13)]


def test_embedding_normalization():
    vector = normalize_embedding([3.0, 4.0] + [0.0] * 190)
    assert len(vector) == 192
    assert vector[:2] == pytest.approx([0.6, 0.8])
    assert math.sqrt(sum(value * value for value in vector)) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="dimensions"):
        normalize_embedding([1.0])


def test_extracts_only_clean_profile_evidence_with_absolute_timestamps(tmp_path: Path):
    path = tmp_path / "meeting.wav"
    embedder = FakeEmbedder()
    segments = [
        Segment(100, 104, "clean", "W02:S01", "participants"),
        Segment(110, 114, "talking", "W02:S01", "participants"),
        Segment(112, 115, "overlap", "W02:S02", "participants"),
    ]
    extracted, diagnostics = extract_window_evidence(
        path, segments, window=2, source_track="participants", embedder=embedder
    )
    assert [(item.start, item.end) for item in extracted["W02:S01"]] == [(100, 104)]
    assert "W02:S02" not in extracted
    assert diagnostics[0]["l2_norm"] == pytest.approx(1.0)
    assert embedder.calls == [(path, 100, 104)]


def test_continuity_when_moss_local_number_changes_and_text_does_not_match():
    windows = [
        [Segment(0, 3, "alpha", "W01:S01", "participants")],
        [Segment(300, 303, "entirely different words", "W02:S09", "participants")],
    ]
    mapping, decisions, profiles = reconcile_speakers(
        windows,
        [evidence(**{"W01:S01": axis(0)}), evidence(**{"W02:S09": axis(0)})],
        role="participants",
    )
    assert mapping["W01:S01"] == mapping["W02:S09"] == "Speaker 1"
    assert decisions[-1]["decision"] == "matched"
    assert len(profiles["Speaker 1"].samples) == 2


def test_existing_profile_is_updated_only_with_selected_clean_samples(tmp_path: Path):
    path = tmp_path / "meeting.wav"
    first_segments = [Segment(0, 4, "first", "W01:S01", "participants")]
    second_segments = [
        Segment(300, 304, "clean", "W02:S07", "participants"),
        Segment(310, 314, "overlap", "W02:S07", "participants"),
        Segment(312, 315, "other", "W02:S08", "participants"),
    ]
    fake = FakeEmbedder({(0, 4): axis(0), (300, 304): axis(0)})
    first_evidence, _ = extract_window_evidence(
        path, first_segments, window=1, source_track="participants", embedder=fake
    )
    second_evidence, _ = extract_window_evidence(
        path, second_segments, window=2, source_track="participants", embedder=fake
    )
    mapping, _, profiles = reconcile_speakers(
        [first_segments, second_segments],
        [first_evidence, second_evidence],
        role="participants",
    )
    assert mapping["W02:S07"] == "Speaker 1"
    assert [(item.start, item.end) for item in profiles["Speaker 1"].samples] == [
        (0, 4),
        (300, 304),
    ]
    assert all(item.quality["overlap_free"] for item in profiles["Speaker 1"].samples)


def test_reconciliation_is_independent_of_transcript_text():
    evidence_windows = [evidence(**{"W01:S01": axis(0)}), evidence(**{"W02:S02": axis(0)})]
    first = [
        [Segment(0, 3, "same text", "W01:S01", "participants")],
        [Segment(300, 303, "same text", "W02:S02", "participants")],
    ]
    second = [
        [Segment(0, 3, "unrelated", "W01:S01", "participants")],
        [Segment(300, 303, "nothing alike", "W02:S02", "participants")],
    ]
    mapping_a, _, _ = reconcile_speakers(first, evidence_windows, role="participants")
    mapping_b, _, _ = reconcile_speakers(second, evidence_windows, role="participants")
    assert mapping_a == mapping_b


def test_reconciles_when_local_speaker_order_changes():
    windows = [
        [
            Segment(0, 3, "a", "W01:S01", "participants"),
            Segment(4, 7, "b", "W01:S02", "participants"),
        ],
        [
            Segment(300, 303, "b", "W02:S01", "participants"),
            Segment(304, 307, "a", "W02:S02", "participants"),
        ],
    ]
    mapping, _, _ = reconcile_speakers(
        windows,
        [
            evidence(**{"W01:S01": axis(0), "W01:S02": axis(1)}),
            evidence(**{"W02:S01": axis(1), "W02:S02": axis(0)}),
        ],
        role="participants",
    )
    assert mapping["W02:S01"] == mapping["W01:S02"]
    assert mapping["W02:S02"] == mapping["W01:S01"]


def test_one_to_one_assignment_prevents_two_locals_sharing_a_profile():
    windows = [
        [Segment(0, 3, "a", "W01:S01", "participants")],
        [
            Segment(300, 303, "a", "W02:S01", "participants"),
            Segment(304, 307, "also a", "W02:S02", "participants"),
        ],
    ]
    mapping, decisions, _ = reconcile_speakers(
        windows,
        [
            evidence(**{"W01:S01": axis(0)}),
            evidence(**{"W02:S01": axis(0), "W02:S02": axis(0)}),
        ],
        role="participants",
    )
    assigned = {mapping["W02:S01"], mapping["W02:S02"]}
    assert len(assigned) == 2
    assert "Speaker 1" in assigned
    assert any(item["reason"] == "one_to_one_conflict" for item in decisions)


def test_creates_new_speaker_below_similarity_threshold():
    windows = [
        [Segment(0, 3, "a", "W01:S01", "participants")],
        [Segment(300, 303, "b", "W02:S01", "participants")],
    ]
    mapping, decisions, _ = reconcile_speakers(
        windows,
        [evidence(**{"W01:S01": axis(0)}), evidence(**{"W02:S01": axis(1)})],
        role="participants",
    )
    assert mapping["W02:S01"] == "Speaker 2"
    assert decisions[-1]["reason"] == "below_similarity_threshold"


def test_creates_new_speaker_when_best_and_second_best_are_too_close():
    windows = [
        [
            Segment(0, 3, "a", "W01:S01", "participants"),
            Segment(4, 7, "b", "W01:S02", "participants"),
        ],
        [Segment(300, 303, "a", "W02:S01", "participants")],
    ]
    mapping, decisions, _ = reconcile_speakers(
        windows,
        [
            evidence(**{"W01:S01": axis(0), "W01:S02": angled(0.98)}),
            evidence(**{"W02:S01": axis(0)}),
        ],
        role="participants",
    )
    assert mapping["W02:S01"] == "Speaker 3"
    assert decisions[-1]["reason"] == "ambiguous_margin"


def test_missing_embedding_creates_new_speaker_and_microphone_stays_remco():
    participant = [[Segment(0, 1, "short", "W01:S01", "participants")]]
    mapping, decisions, profiles = reconcile_speakers(participant, [{}], role="participants")
    assert mapping["W01:S01"] == "Speaker 1"
    assert decisions[0]["reason"] == "no_clean_embedding"
    assert profiles["Speaker 1"].samples == []

    microphone = [[Segment(0, 3, "mine", "Remco", "microphone")]]
    mapping, decisions, profiles = reconcile_speakers(microphone, [{}], role="microphone")
    assert mapping == {"Remco": "Remco"}
    assert decisions == []
    assert profiles == {}


def test_microphone_evidence_is_retained_as_a_voiceprint_profile():
    microphone = [[Segment(0, 3, "mine", "Remco", "microphone")]]
    mapping, decisions, profiles = reconcile_speakers(
        microphone,
        [evidence(Remco=axis(0))],
        role="microphone",
    )
    assert mapping == {"Remco": "Remco"}
    assert decisions == []
    assert len(profiles["Remco"].samples) == 1


def test_voiceprints_collapse_samples_to_one_normalized_embedding_per_handle(tmp_path: Path):
    profiles = {
        "speaker-2": SpeakerProfile(
            speaker="speaker-2",
            model="ReDimNet2",
            model_revision="revision",
            checkpoint_sha256="a" * 64,
            embedding_dimension=192,
            samples=[sample(axis(0)), sample(angled(0.9)), sample(axis(1))],
        ),
        "speaker-1": SpeakerProfile(
            speaker="speaker-1",
            model="ReDimNet2",
            model_revision="revision",
            checkpoint_sha256="a" * 64,
            embedding_dimension=192,
            samples=[sample(axis(2))],
        ),
        "speaker-3": SpeakerProfile(
            speaker="speaker-3",
            model="ReDimNet2",
            model_revision="revision",
            checkpoint_sha256="a" * 64,
            embedding_dimension=192,
            samples=[],
        ),
    }

    handles, embeddings = aggregate_voiceprints(profiles)

    assert handles.tolist() == ["speaker-1", "speaker-2"]
    assert embeddings.shape == (2, 192)
    assert np.linalg.norm(embeddings, axis=1).tolist() == pytest.approx([1.0, 1.0])
    assert embeddings[1, 0] > embeddings[1, 1]

    path = tmp_path / "meeting.voiceprints.npz"
    write_voiceprints(profiles, path)
    with np.load(path, allow_pickle=False) as voiceprints:
        assert voiceprints.files == ["handles", "embeddings"]
        assert voiceprints["handles"].dtype.kind == "U"
        assert voiceprints["embeddings"].dtype == np.float32


def test_empty_voiceprints_have_stable_shapes(tmp_path: Path):
    path = tmp_path / "empty.voiceprints.npz"
    write_voiceprints({}, path)
    with np.load(path, allow_pickle=False) as voiceprints:
        assert voiceprints["handles"].shape == (0,)
        assert voiceprints["embeddings"].shape == (0, 192)
