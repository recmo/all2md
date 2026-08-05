from __future__ import annotations

from pathlib import Path
import socket

import pytest

from speech_review.server import ReviewServer


def test_port_conflict_preserves_original_bind_error(tmp_path: Path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        with pytest.raises(OSError, match="Address already in use"):
            ReviewServer(listener.getsockname(), tmp_path)
