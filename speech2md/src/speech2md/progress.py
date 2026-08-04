from __future__ import annotations

import json
import os
from typing import Any


PROGRESS_FD_ENV = "SPEECH2MD_PROGRESS_FD"


def emit_progress(stage: str, **details: Any) -> None:
    """Emit one internal JSON progress event when a caller provides a pipe."""
    raw_fd = os.environ.get(PROGRESS_FD_ENV)
    if raw_fd is None:
        return
    try:
        fd = int(raw_fd)
        payload = json.dumps({"stage": stage, **details}, ensure_ascii=False) + "\n"
        os.write(fd, payload.encode())
    except (OSError, TypeError, ValueError):
        return
