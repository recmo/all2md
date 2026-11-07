"""Credential-free, persistent inference with cancellable job execution."""
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
import threading
import time

from . import recipes, text


class Inference:
    def __init__(self, selected, device, token_env, *, idle_seconds=None):
        self.selected = selected
        self.device = device
        self.token_env = token_env
        self.process = None
        self.connection = None
        self.idle_seconds = idle_seconds
        self._lock = threading.RLock()
        self._timer = None
        self._deadline = None

    def _expire(self):
        with self._lock:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                self.close()

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        self._deadline = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
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
                raise RuntimeError("inference process exited")
        if lost.is_set():
            raise RuntimeError("worker lease lost")
        return json.loads(self.connection.recv_bytes())

    def start(self, lost):
        with self._lock:
            self._start(lost)

    def _start(self, lost):
        if self.process is not None and self.process.poll() is not None:
            self.close()
        if self.process is not None:
            return
        parent, child = socket.socketpair()
        self.connection = Connection(parent.detach())
        try:
            command = [sys.executable, "-m", "ml_worker.worker"]
            if recipes.SPEECH_RECIPE in self.selected:
                # The Nix speech interpreter owns MLX and torchaudio. Import only
                # our package into that environment, without shadowing its deps.
                bootstrap = (
                    "import importlib.util, runpy, sys; "
                    "spec = importlib.util.spec_from_file_location('ml_worker', sys.argv.pop(1)); "
                    "module = importlib.util.module_from_spec(spec); "
                    "sys.modules['ml_worker'] = module; spec.loader.exec_module(module); "
                    "runpy.run_module('ml_worker.worker', run_name='__main__')"
                )
                command = [os.environ.get("ML_WORKER_SPEECH_PYTHON", sys.executable), "-c", bootstrap,
                           str(Path(__file__).with_name("__init__.py"))]
            self.process = subprocess.Popen(
                [*command, "--inference-fd", str(child.fileno()),
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
        with self._lock:
            self._deadline = None
            if self._timer is not None:
                self._timer.cancel()
            try:
                self._execute(request, lock_fd, lost)
            finally:
                if self.process is not None and self.idle_seconds is not None:
                    self._deadline = time.monotonic() + self.idle_seconds
                    self._timer = threading.Timer(self.idle_seconds, self._expire)
                    self._timer.daemon = True
                    self._timer.start()

    def _execute(self, request, lock_fd, lost):
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
