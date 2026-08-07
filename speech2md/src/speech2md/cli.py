from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .moss_cache import MossCacheMiss
from .pipeline import transcribe


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="speech2md",
        description="Transcribe local meeting audio with MOSS and ReDimNet2",
    )
    command.add_argument("--version", action="version", version=f"speech2md {__version__}")
    command.add_argument("input", type=Path)
    command.add_argument("--force", action="store_true", help="replace existing derived output")
    command.add_argument("--require-moss-cache", action="store_true", help=argparse.SUPPRESS)
    return command


def main(argv: list[str] | None = None) -> int:
    command = parser()
    arguments = command.parse_args(argv)
    try:
        state = transcribe(
            arguments.input,
            force=arguments.force,
            require_moss_cache=arguments.require_moss_cache,
        )
        print(json.dumps({
            "segments": len(state.segments),
            "processing_seconds": state.processing_seconds,
        }))
    except MossCacheMiss:
        return 75
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        command.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
