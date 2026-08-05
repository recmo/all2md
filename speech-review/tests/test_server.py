from __future__ import annotations

from pathlib import Path
import socket

import pytest

from speech_review.server import ReviewHandler, ReviewServer


def test_port_conflict_preserves_original_bind_error(tmp_path: Path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        with pytest.raises(OSError, match="Address already in use"):
            ReviewServer(listener.getsockname(), tmp_path)


@pytest.mark.parametrize("disconnect", [BrokenPipeError, ConnectionResetError])
def test_audio_stream_ignores_client_disconnect(tmp_path: Path, disconnect: type[OSError]):
    source = tmp_path / "audio.mp4"
    source.write_bytes(b"audio")

    class Handler:
        headers = {"Range": "bytes=0-"}

        def send_response(self, _status):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

        class Writer:
            def write(self, _chunk):
                raise disconnect()

        wfile = Writer()

    ReviewHandler._file(Handler(), source)
