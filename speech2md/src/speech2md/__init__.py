"""Local MOSS meeting transcription."""

import os
from pathlib import Path
import subprocess


def commit_version() -> str:
    injected = os.environ.get("SPEECH2MD_VERSION")
    if injected:
        return injected
    try:
        return subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown"


__version__ = commit_version()
