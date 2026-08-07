from __future__ import annotations

import json
import os

from speech2md.progress import PROGRESS_FD_ENV, emit_progress


def test_progress_events_are_optional_and_structured(monkeypatch):
    emit_progress("ignored", completed_seconds=0)
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))
    emit_progress("transcribing", completed_seconds=12.5, total_seconds=100)
    os.close(write_fd)
    with os.fdopen(read_fd) as stream:
        event = json.loads(stream.readline())
    assert event == {
        "stage": "transcribing",
        "completed_seconds": 12.5,
        "total_seconds": 100,
    }
