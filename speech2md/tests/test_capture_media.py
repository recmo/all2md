"""Exercise the multi-process archive contract without loading speech models."""
from array import array
import json
from pathlib import Path
import subprocess

import pytest

from speech2md.media import probe, resolve_input, sha256


def test_process_archive_tracks_remain_distinct(tmp_path: Path):
    archive = tmp_path / "meeting.mka"
    arguments = ["ffmpeg", "-nostdin", "-v", "error"]
    for frequency in (300, 600, 900):
        arguments += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=0.3"]
    arguments += ["-map", "0:a", "-map", "1:a", "-map", "2:a", "-c:a", "libopus", str(archive)]
    subprocess.run(arguments, check=True)
    checksum = sha256(archive)
    manifest = tmp_path / "meeting-capture.json"
    tracks = [
        {"streamIndex": 0, "role": "microphone"},
        {"streamIndex": 1, "role": "process", "id": "first", "process": {"processID": 42, "name": "FaceTime"}},
        {"streamIndex": 2, "role": "process", "id": "second", "process": {"processID": 43, "name": "Browser"}},
    ]
    value = {
        "schemaVersion": 3, "meetingID": "fixture", "startedAt": "now", "endedAt": "later",
        "status": "complete", "container": {"file": archive.name, "sha256": checksum}, "audio": tracks,
    }
    manifest.write_text(json.dumps(value))
    resolved = resolve_input(manifest)
    assert [source[1] for source in resolved.sources] == ["microphone", "process-first", "process-second"]
    for (path, role, expected, index), frequency in zip(resolved.sources, (300, 600, 900), strict=True):
        source = probe(path, role=role, expected_sha256=expected, stream_index=index)
        assert source.duration_seconds > 0
        assert source.role == role
        decoded = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:a:{index}",
            "-ac", "1", "-ar", "8000", "-f", "f32le", "-",
        ], check=True, capture_output=True).stdout
        samples = array("f", decoded)
        crossings = sum((a < 0) != (b < 0) for a, b in zip(samples, samples[1:]))
        measured = crossings * 8000 / (2 * len(samples))
        assert abs(measured - frequency) < 30

    tracks[2]["id"] = "first"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="identities must be unique"):
        resolve_input(manifest)
    del tracks[2]["id"]
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="missing its identity"):
        resolve_input(manifest)
