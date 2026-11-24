"""Exercise current and previous capture archives without loading speech models."""
from array import array
import json
from pathlib import Path
import subprocess

import pytest

from speech2md.media import probe, resolve_input, sha256


@pytest.mark.parametrize(
    ("output_tracks", "roles"),
    [
        ([{"role": "system"}], ["microphone", "system"]),
        ([
            {"role": "process", "id": "first", "process": {"processID": 42}},
            {"role": "process", "id": "second", "process": {"processID": 43}},
        ], ["microphone", "process-first", "process-second"]),
    ],
    ids=["system", "per-process"],
)
def test_capture_archive_tracks_remain_distinct(tmp_path: Path, output_tracks, roles):
    archive = tmp_path / "meeting.mka"
    frequencies = (300, 600, 900)[:len(roles)]
    arguments = ["ffmpeg", "-nostdin", "-v", "error"]
    for frequency in frequencies:
        arguments += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=0.3"]
    for index in range(len(frequencies)):
        arguments += ["-map", f"{index}:a"]
    subprocess.run(arguments + ["-c:a", "libopus", str(archive)], check=True)
    checksum = sha256(archive)
    manifest = tmp_path / "meeting-capture.json"
    tracks = [
        dict(track, streamIndex=index)
        for index, track in enumerate([{"role": "microphone"}, *output_tracks])
    ]
    value = {
        "schemaVersion": 3, "meetingID": "fixture", "startedAt": "now", "endedAt": "later",
        "status": "complete", "container": {"file": archive.name, "sha256": checksum}, "audio": tracks,
    }
    manifest.write_text(json.dumps(value))
    resolved = resolve_input(manifest)
    assert [source[1] for source in resolved.sources] == roles
    for (path, role, expected, index), frequency in zip(resolved.sources, frequencies, strict=True):
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

    tracks[-1] = dict(tracks[-2], streamIndex=tracks[-1]["streamIndex"])
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="identities must be unique"):
        resolve_input(manifest)
