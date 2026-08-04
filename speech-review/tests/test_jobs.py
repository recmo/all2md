from __future__ import annotations

from pathlib import Path
import sys
import time

from speech_review.jobs import RegenerationQueue
from speech_review.transcripts import TranscriptFile


def test_queue_deduplicates_active_recording_and_reports_position(tmp_path: Path):
    source = tmp_path / "meeting.mp4"
    source.touch()
    transcript = TranscriptFile(tmp_path, tmp_path / "meeting.md", source)
    jobs = RegenerationQueue(tmp_path, start_worker=False)
    first = jobs.enqueue(transcript)
    second = jobs.enqueue(transcript)

    assert first is second
    assert jobs.payload()[0] == {
        **first.payload(),
        "position": 1,
    }
    jobs.close()


def test_queue_updates_structured_audio_progress(tmp_path: Path):
    source = tmp_path / "meeting.mp4"
    source.touch()
    transcript = TranscriptFile(tmp_path, tmp_path / "meeting.md", source)
    jobs = RegenerationQueue(tmp_path, start_worker=False)
    job = jobs.enqueue(transcript)

    jobs._update_progress(
        job,
        '{"stage":"mixed window 2/4","completed_seconds":50,"total_seconds":200}',
    )

    assert job.stage == "mixed window 2/4"
    assert job.progress == 0.25
    jobs.close()


def test_worker_consumes_progress_pipe_and_completes(tmp_path: Path):
    source = tmp_path / "meeting.mp4"
    source.touch()
    transcript = TranscriptFile(tmp_path, tmp_path / "meeting.md", source)
    program = (
        "import json, os; "
        "fd=int(os.environ['SPEECH2MD_PROGRESS_FD']); "
        "os.write(fd, (json.dumps({'stage':'mixed window 1/1',"
        "'completed_seconds':10,'total_seconds':10})+'\\n').encode())"
    )
    jobs = RegenerationQueue(tmp_path, command=(sys.executable, "-c", program))
    job = jobs.enqueue(transcript)
    deadline = time.monotonic() + 5
    while job.status in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.01)

    assert job.status == "complete"
    assert job.stage == "Complete"
    assert job.progress == 1.0
    jobs.close()


def test_cached_reprocessing_starts_without_waiting_in_queue(tmp_path: Path):
    source = tmp_path / "meeting.mp4"
    source.touch()
    transcript = TranscriptFile(tmp_path, tmp_path / "meeting.md", source)
    program = (
        "import json, os, sys; "
        "assert '--require-moss-cache' in sys.argv; "
        "fd=int(os.environ['SPEECH2MD_PROGRESS_FD']); "
        "os.write(fd, (json.dumps({'stage':'replaying cached MOSS',"
        "'completed_seconds':10,'total_seconds':10})+'\\n').encode())"
    )
    jobs = RegenerationQueue(tmp_path, command=(sys.executable, "-c", program))

    job = jobs.enqueue(transcript, prefer_cache=True)
    deadline = time.monotonic() + 5
    while job.status in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.01)

    assert job.status == "complete"
    assert job.mode == "cached"
    assert "position" not in jobs.payload()[0]
    jobs.close()


def test_cache_miss_falls_back_to_serial_queue(tmp_path: Path):
    source = tmp_path / "meeting.mp4"
    source.touch()
    transcript = TranscriptFile(tmp_path, tmp_path / "meeting.md", source)
    program = (
        "import json, os, sys; "
        "sys.exit(75) if '--require-moss-cache' in sys.argv else None; "
        "fd=int(os.environ['SPEECH2MD_PROGRESS_FD']); "
        "os.write(fd, (json.dumps({'stage':'transcribing',"
        "'completed_seconds':10,'total_seconds':10})+'\\n').encode())"
    )
    jobs = RegenerationQueue(tmp_path, command=(sys.executable, "-c", program))

    job = jobs.enqueue(transcript, prefer_cache=True)
    deadline = time.monotonic() + 5
    while job.status in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.01)

    assert job.status == "complete"
    assert job.mode == "full"
    jobs.close()
