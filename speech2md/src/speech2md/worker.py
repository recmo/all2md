"""Pull leased transcription jobs from mdstore; never write its repository directly."""
from __future__ import annotations

import argparse
import hashlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class Client:
    def __init__(self, server: str, token: str):
        parsed = urlparse(server)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("server must be an HTTP(S) origin")
        if not token:
            raise ValueError("worker token is required")
        self.server = server.rstrip("/")
        self.token = token

    def request(self, path: str, value=None, *, body: bytes | None = None, method: str | None = None):
        headers = {"Authorization": f"Bearer {self.token}"}
        if value is not None:
            body = json.dumps(value, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.server + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read(8192).decode("utf-8", errors="replace")
            raise RuntimeError(f"mdstore returned {error.code}: {detail}") from error

    def download(self, query: dict, target: Path, asset: dict):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and digest(target) == asset["oid"]:
            return
        temporary = target.with_name(target.name + ".part")
        request = Request(self.server + "/worker/inputs?" + urlencode(query), headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    if output.tell() > asset["size"]:
                        raise ValueError("download exceeds assigned size")
            if temporary.stat().st_size != asset["size"] or digest(temporary) != asset["oid"]:
                raise ValueError("assigned input checksum mismatch")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_path(root: Path, path: str) -> Path:
    target = (root / path).resolve()
    if not path or Path(path).is_absolute() or not target.is_relative_to(root.resolve()):
        raise ValueError("job path escapes the worker directory")
    return target


def execute(client: Client, assignment: dict, cache: Path):
    job = assignment["job"]
    # Include the server identity so jobs from separate stores cannot share paths.
    scope = hashlib.sha256(client.server.encode()).hexdigest()
    root = local_path(cache.expanduser().resolve() / scope, job["id"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".worker.lock").open("a") as lock:
        # A sleeping process can still hold this cache after its server lease expires.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _execute_locked(client, assignment, root)


def _execute_locked(client: Client, assignment: dict, root: Path):
    job = assignment["job"]
    attempt = job["attempt"]
    endpoint = f"/worker/jobs/{job['id']}"
    stopped = threading.Event()
    lease_lost = threading.Event()
    progress = [{"stage": "downloading inputs"}]

    def heartbeat():
        while not stopped.wait(30):
            try:
                client.request(endpoint + "/heartbeat", {"attempt": attempt, "progress": progress[0]})
            except Exception:
                lease_lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        recording = local_path(root, job["source"])
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(assignment["recording"], encoding="utf-8")
        for path, asset in assignment["inputs"].items():
            client.download({"job": job["id"], "attempt": attempt, "path": path}, local_path(root, path), asset)
        from .recording import frontmatter, resolve_recording
        resolved = resolve_recording(recording, frontmatter(recording))
        assigned = {local_path(root, path) for path in assignment["inputs"]}
        for source, _, _, _ in resolved.sources:
            if source.resolve() not in assigned:
                raise ValueError("capture manifest references an unassigned source")
        expected = {str(resolved.markdown_path.relative_to(root)), str(resolved.markdown_path.with_suffix('.voiceprints.json').relative_to(root))}
        if set(assignment["outputs"]) != expected:
            raise ValueError("speech2md output paths do not match the assignment")
        read_fd, write_fd = os.pipe()
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "speech2md.cli", str(recording), "--force"],
                env={**os.environ, "SPEECH2MD_PROGRESS_FD": str(write_fd)}, pass_fds=(write_fd,),
            )
        except Exception:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        def read_progress():
            with os.fdopen(read_fd) as events:
                for line in events:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            progress[0] = event
                    except ValueError:
                        continue
        progress_thread = threading.Thread(target=read_progress, daemon=True)
        progress_thread.start()
        try:
            while process.poll() is None:
                if lease_lost.wait(1):
                    raise RuntimeError("worker lease lost")
            if process.returncode:
                raise RuntimeError(f"speech2md exited with status {process.returncode}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            progress_thread.join(timeout=2)
        if lease_lost.is_set():
            raise RuntimeError("worker lease lost")
        outputs, assets = {}, {}
        for path in assignment["outputs"]:
            if not path.endswith(".voiceprints.json"):
                continue
            target = local_path(root, path)
            voiceprints = json.loads(target.read_text())
            for record in voiceprints["records"]:
                if record["metadata"].get("confirmed"):
                    continue
                record["metadata"]["suggestions"] = client.request("/worker/vectors/search", {
                    "job": job["id"], "attempt": attempt, "space": voiceprints["space"],
                    "vector": record["vector"], "limit": 5, "filter": {"confirmed": True},
                })
            target.write_text(json.dumps(voiceprints, allow_nan=False), encoding="utf-8")
        for path in assignment["outputs"]:
            target = local_path(root, path)
            if path.endswith(".md"):
                outputs[path] = target.read_text(encoding="utf-8")
            else:
                assets[path] = client.request("/worker/outputs?" + urlencode({"job": job["id"], "attempt": attempt, "path": path}), body=target.read_bytes(), method="PUT")
        client.request(endpoint + "/complete", {"attempt": attempt, "outputs": outputs, "assets": assets})
    except Exception as error:
        if not lease_lost.is_set():
            try:
                client.request(endpoint + "/heartbeat", {"attempt": attempt, "failure": str(error)[:2000]})
            except Exception:
                pass
        raise
    finally:
        stopped.set()
        thread.join(timeout=65)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--token-env", default="MDSTORE_WORKER_TOKEN")
    parser.add_argument("--recipe", default="speech2md-v1")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/speech2md/worker")
    parser.add_argument("--once", action="store_true", help="claim at most one job and exit")
    args = parser.parse_args(argv)
    client = Client(args.server, os.environ.get(args.token_env, ""))
    while True:
        try:
            assignment = client.request("/worker/claim", {"recipes": [args.recipe]})
            if assignment:
                execute(client, assignment, args.cache)
            if args.once:
                return 0
        except Exception as error:
            print(f"worker: {error}", file=sys.stderr)
            if args.once:
                return 1
        time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
