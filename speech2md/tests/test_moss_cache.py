from pathlib import Path

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
    changed = cache_metadata(prompt="Prompt: ProveKit", hotwords=("ProveKit",), sources=[source])
    assert load_cache(path, changed) == {}
    assert path.stat().st_mode & 0o777 == 0o600
