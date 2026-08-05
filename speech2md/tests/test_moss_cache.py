from pathlib import Path
import copy

from speech2md.model import AudioSource
from speech2md.moss_cache import cache_metadata, load_cache, source_key, write_cache


def test_moss_cache_round_trip_and_invalidation(tmp_path: Path):
    path = tmp_path / "meeting.moss.npz"
    source = AudioSource("/meeting.wav", "mixed", "a" * 64, 60, "wav")
    metadata = cache_metadata(prompt="Prompt: F2Z", hotwords=("F2Z",), sources=[source])
    tracks = {source_key(source): [{
        "text": "[0][S01]F2Z[2]",
        "prompt_tokens": 10,
        "generation_tokens": 4,
        "total_tokens": 14,
    }]}

    write_cache(path, metadata, tracks)

    assert load_cache(path, metadata) == tracks
    metadata_with_commit = {**metadata, "speech2md_version": "b" * 40}
    write_cache(path, metadata_with_commit, tracks)
    assert load_cache(path, metadata) == tracks
    assert load_cache(path, {**metadata, "speech2md_version": "c" * 40}) == tracks
    changed = cache_metadata(prompt="Prompt: ProveKit", hotwords=("ProveKit",), sources=[source])
    assert load_cache(path, changed) == {}
    assert path.stat().st_mode & 0o777 == 0o600


def test_moss_cache_round_trips_guided_generation(tmp_path: Path):
    path = tmp_path / "meeting.moss.npz"
    source = AudioSource("/meeting.wav", "mixed", "a" * 64, 60, "wav")
    metadata = cache_metadata(prompt="Prompt", hotwords=(), sources=[source])
    base = {
        "text": "[0][S01]Alice[2][2][S01]Bob[4]",
        "prompt_tokens": 10,
        "generation_tokens": 8,
        "total_tokens": 18,
    }
    tracks = {source_key(source): [{
        "text": "[0][S01]Alice[2][2][S02]Bob[4]",
        "prompt_tokens": 10,
        "generation_tokens": 8,
        "total_tokens": 18,
        "base": base,
        "speaker_forces": [{"start": 2, "speaker": "S02", "identity": "Bob"}],
    }]}

    write_cache(path, metadata, tracks)

    assert load_cache(path, metadata) == tracks

    missing_base = copy.deepcopy(tracks)
    del missing_base[source_key(source)][0]["base"]
    write_cache(path, metadata, missing_base)
    assert load_cache(path, metadata) == {}

    tracks[source_key(source)][0]["speaker_forces"][0]["start"] = float("nan")
    write_cache(path, metadata, tracks)
    assert load_cache(path, metadata) == {}
