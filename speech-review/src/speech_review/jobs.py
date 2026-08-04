from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import uuid

from .transcripts import TranscriptFile


ACTIVE = {"queued", "running"}


@dataclass
class Job:
    id: str
    transcript_id: str
    name: str
    requested: Path
    status: str = "queued"
    stage: str = "Waiting"
    progress: float = 0.0
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def payload(self) -> dict:
        return {
            "id": self.id,
            "transcriptId": self.transcript_id,
            "name": self.name,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "error": self.error,
        }


class RegenerationQueue:
    def __init__(
        self,
        root: Path,
        *,
        start_worker: bool = True,
        command: tuple[str, ...] = ("speech2md",),
    ):
        self.root = root
        self.command = command
        self._jobs: dict[str, Job] = {}
        self._pending: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._worker = None
        if start_worker:
            self._worker = threading.Thread(target=self._work, name="speech-review-jobs", daemon=True)
            self._worker.start()

    def enqueue(self, transcript: TranscriptFile) -> Job:
        if transcript.requested is None:
            raise ValueError("the original speech2md input is unavailable")
        with self._lock:
            for job in self._jobs.values():
                if job.transcript_id == transcript.identifier and job.status in ACTIVE:
                    return job
            job = Job(
                id=uuid.uuid4().hex,
                transcript_id=transcript.identifier,
                name=str(transcript.relative),
                requested=transcript.requested,
            )
            self._jobs[job.id] = job
        self._pending.put(job.id)
        return job

    def payload(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.created_at)
            queued = [job for job in jobs if job.status == "queued"]
            positions = {job.id: index + 1 for index, job in enumerate(queued)}
            values = []
            for job in jobs:
                value = job.payload()
                if job.id in positions:
                    value["position"] = positions[job.id]
                values.append(value)
            return values

    def close(self) -> None:
        with self._lock:
            process = self._process
            for job in self._jobs.values():
                if job.status == "queued":
                    job.status = "cancelled"
                    job.stage = "Server stopped"
        if process and process.poll() is None:
            process.terminate()
        self._pending.put(None)

    def _work(self) -> None:
        while True:
            job_id = self._pending.get()
            if job_id is None:
                return
            with self._lock:
                job = self._jobs[job_id]
                if job.status != "queued":
                    continue
                job.status = "running"
                job.stage = "Starting speech2md"
            self._run(job)

    def _run(self, job: Job) -> None:
        read_fd, write_fd = os.pipe()
        environment = os.environ.copy()
        environment["SPEECH2MD_PROGRESS_FD"] = str(write_fd)
        try:
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(
                    [*self.command, str(job.requested), "--force"],
                    cwd=self.root,
                    env=environment,
                    pass_fds=(write_fd,),
                    stdout=output,
                    stderr=output,
                )
                with self._lock:
                    self._process = process
                os.close(write_fd)
                write_fd = -1
                with os.fdopen(read_fd) as progress:
                    read_fd = -1
                    for line in progress:
                        self._update_progress(job, line)
                return_code = process.wait()
                output.seek(0)
                log = output.read().decode(errors="replace").strip()
            with self._lock:
                if return_code == 0:
                    job.status = "complete"
                    job.stage = "Complete"
                    job.progress = 1.0
                else:
                    job.status = "failed"
                    job.stage = "Failed"
                    job.error = log[-2000:] or f"speech2md exited with status {return_code}"
        except Exception as error:
            with self._lock:
                job.status = "failed"
                job.stage = "Failed"
                job.error = str(error)
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            if read_fd >= 0:
                os.close(read_fd)
            with self._lock:
                self._process = None

    def _update_progress(self, job: Job, line: str) -> None:
        try:
            event = json.loads(line)
            stage = str(event.get("stage", "Processing"))
            completed = float(event.get("completed_seconds", 0))
            total = float(event.get("total_seconds", 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        with self._lock:
            job.stage = stage
            if total > 0:
                job.progress = max(0.0, min(1.0, completed / total))
