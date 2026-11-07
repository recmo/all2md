"""Pull leased ML jobs from mdstore and publish only their declared outputs."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import shutil
import signal
import sys
import threading
import tempfile
import time
from urllib.parse import quote, urlencode

from .client import Client, local_path
from . import audio, recipes, text
from .models import Models

MAX_OUTPUT = 64 * 1024 * 1024


def capabilities():
    result = []
    if importlib.util.find_spec("speech2md"):
        result.append(recipes.SPEECH_RECIPE)
    if shutil.which("ffmpeg"):
        result.append(recipes.LEVELS_RECIPE)
        if shutil.which("deep-filter-worker"):
            result.append(audio.RECIPE)
    supported = sys.platform == "darwin" and platform.machine() == "arm64"
    if supported and importlib.util.find_spec("mlx_lm") and importlib.util.find_spec("transformers"):
        result.extend((text.EMBED_RECIPE, text.RERANK_RECIPE))
    return result


def execute(client, assignment, cache, *, models, heartbeat_seconds=30):
    job = assignment["job"]
    scope = hashlib.sha256(client.server.encode()).hexdigest()
    cache_root = cache.expanduser().resolve() / scope
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if assignment.get("retrieval", False):
        with tempfile.TemporaryDirectory(prefix="retrieval-", dir=cache_root) as directory:
            root = Path(directory) / "work"
            root.mkdir(mode=0o700)
            return _execute_locked(client, assignment, root, heartbeat_seconds, models)
    identity = hashlib.sha256(job["id"].encode()).hexdigest()
    job_root = cache_root / identity
    job_root.mkdir(exist_ok=True, mode=0o700)
    with (job_root / ".worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fingerprint = job["fingerprint"]
        # Hash the label rather than trusting a server string as a cache path.
        root = job_root / hashlib.sha256(fingerprint.encode()).hexdigest()
        # Retain this workspace for retries and the shared MOSS cache at job_root.
        for previous in job_root.iterdir():
            if previous.is_dir() and previous != root:
                shutil.rmtree(previous)
        root.mkdir(exist_ok=True, mode=0o700)
        _execute_locked(client, assignment, root, heartbeat_seconds, models)


def _execute_locked(client, assignment, root, heartbeat_seconds, models):
    job = assignment["job"]
    lease = {"id": job["id"], "attempt": job["attempt"]}
    scratch = root.parent / ".scratch"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(mode=0o700)
    stopped, lost = threading.Event(), threading.Event()
    progress = {"stage": "downloading inputs", "recipe": job["recipe"]}
    def heartbeat():
        while not stopped.wait(heartbeat_seconds):
            try:
                client.request("/worker", {"op": "heartbeat", **lease, "progress": dict(progress)})
            except Exception:
                lost.set()
                return
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    def check_lease():
        if lost.is_set():
            raise RuntimeError("worker lease lost")
    try:
        source, _, outputs = recipes.validate_assignment(assignment, root)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(assignment["recording"], encoding="utf-8")
        for path, asset in assignment["inputs"].items():
            check_lease()
            client.download({"job": job["id"], "attempt": job["attempt"], "path": path},
                            local_path(root, path), asset)
        for output in outputs:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.unlink(missing_ok=True)
        progress["stage"] = "processing"
        check_lease()
        with models.use(job["recipe"]):
            recipes.execute(assignment, root, scratch=scratch)
        check_lease()
        progress["stage"] = "publishing"
        for output in outputs:
            if not output.is_file() or output.stat().st_size > MAX_OUTPUT:
                raise ValueError("missing output or output exceeds mdstore's 64 MiB limit")
        for path in assignment["outputs"]:
            if not path.endswith(".voiceprints.json"):
                continue
            target = local_path(root, path)
            voiceprints = json.loads(target.read_text())
            for record in voiceprints["records"]:
                if not record["metadata"].get("confirmed"):
                    record["metadata"]["suggestions"] = client.search({
                        "job": job["id"], "attempt": job["attempt"], "space": voiceprints["space"],
                        "vector": record["vector"], "limit": 5, "filter": {"confirmed": True}})
            recipes.json_output(target, voiceprints)
        texts, assets = {}, {}
        for path in assignment["outputs"]:
            check_lease()
            target = local_path(root, path)
            if target.stat().st_size > MAX_OUTPUT:
                raise ValueError("output exceeds mdstore's 64 MiB limit")
            if path.endswith(".md") or assignment.get("retrieval", False):
                texts[path] = target.read_text(encoding="utf-8")
            else:
                assets[path] = client.request("/" + quote(path, safe="/") + "?" + urlencode({
                    "job": job["id"], "attempt": job["attempt"]}), body=target.read_bytes(), method="PUT")
        check_lease()
        client.request("/worker", {"op": "complete", **lease, "outputs": texts, "assets": assets})
    except Exception as error:
        if not lost.is_set():
            try:
                client.request("/worker", {"op": "heartbeat", **lease, "failure": str(error)[:2000]})
            except Exception:
                pass
        raise
    finally:
        stopped.set()
        thread.join(timeout=65)
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server")
    parser.add_argument("--token-env", default="MDSTORE_WORKER_TOKEN")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/ml-worker")
    parser.add_argument("--recipes", nargs="+", choices=recipes.RECIPES)
    parser.add_argument("--list-recipes", action="store_true")
    parser.add_argument("--once", action="store_true", help="claim at most one job and exit")
    args = parser.parse_args(argv)
    available = capabilities()
    if args.list_recipes:
        print("\n".join(available))
        return 0
    selected = args.recipes or available
    if not args.server or not selected or set(selected)-set(available):
        parser.error("--server and locally available recipes are required; use --list-recipes")
    client = Client(args.server, os.environ.get(args.token_env, ""))
    # Keep the credential in Client, out of environments inherited by audio tools.
    os.environ.pop(args.token_env, None)
    os.environ.pop("MDSTORE_WORKER_TOKEN", None)
    models = Models()
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    try:
        text.preload(selected)
        while True:
            try:
                assignment = client.request("/worker", {"op": "claim", "recipes": selected})
                if assignment:
                    execute(client, assignment, args.cache, models=models)
                if args.once:
                    return 0
                if assignment:
                    continue
            except Exception as error:
                print(f"worker: {error}", file=sys.stderr)
                if args.once:
                    return 1
                time.sleep(10)
                continue
            time.sleep(1)
    except KeyboardInterrupt:
        return 130
    finally:
        models.close()
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
