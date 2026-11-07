"""Credential-free, persistent text inference with cancellable job execution."""
from __future__ import annotations

import json
from multiprocessing.connection import Connection
from multiprocessing.reduction import recv_handle, send_handle
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

from . import recipes, text


class Inference:
    def __init__(self, selected, device, token_env):
        self.selected = selected
        self.device = device
        self.token_env = token_env
        self.process = None
        self.connection = None

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _receive(self, lost):
        while not self.connection.poll(0.2):
            if lost.is_set():
                raise RuntimeError("worker lease lost")
            if self.process.poll() is not None:
                raise RuntimeError("text inference process exited")
        if lost.is_set():
            raise RuntimeError("worker lease lost")
        return json.loads(self.connection.recv_bytes())

    def start(self, lost):
        if self.process is not None and self.process.poll() is not None:
            self.close()
        if self.process is not None:
            return
        parent, child = socket.socketpair()
        self.connection = Connection(parent.detach())
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "ml_worker.worker", "--inference-fd", str(child.fileno()),
                 "--device", self.device, "--recipes", *self.selected],
                env={k: v for k, v in os.environ.items()
                     if k not in {self.token_env, "MDSTORE_WORKER_TOKEN"}},
                stdin=subprocess.DEVNULL, start_new_session=True, pass_fds=(child.fileno(),),
            )
            self._receive(lost)
        except BaseException:
            self.close()
            raise
        finally:
            child.close()

    def execute(self, request, lock_fd, lost):
        try:
            self.start(lost)
            self.connection.send_bytes(json.dumps([str(request), lock_fd is not None]).encode())
            if lock_fd is not None:
                # Share the same flock description, keeping the workspace locked
                # even if the controller dies during inference.
                send_handle(self.connection, lock_fd, self.process.pid)
            error = self._receive(lost)
        except BaseException:
            self.close()
            raise
        if error is not None:
            raise RuntimeError(error)


def serve(fd, selected, device):
    with Connection(fd) as connection:
        text.preload(selected, device=device)
        connection.send_bytes(b"null")
        while True:
            try:
                request, has_lock = json.loads(connection.recv_bytes())
            except EOFError:
                return
            lock_fd = recv_handle(connection) if has_lock else None
            error = None
            try:
                request = Path(request)
                recipes.execute(json.loads(request.read_text()), request.parent, device=device,
                                scratch=request.parent.parent / ".scratch")
            except Exception as exc:
                error = str(exc)[:2000]
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
            connection.send_bytes(json.dumps(error).encode())
