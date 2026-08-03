from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .benchmark import summarize
from .pipeline import relabel, render, transcribe


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="speech2md",
        description="Transcribe local meeting audio with MOSS and reconcile speakers with ReDimNet2",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    command = commands.add_parser("transcribe", help="transcribe a capture manifest or media file")
    command.add_argument("input", type=Path)
    command.add_argument("--force", action="store_true", help="replace derived artifacts; canonical audio is never changed")
    command.add_argument(
        "--hotwords",
        metavar="TERM,...",
        help="comma-separated names and domain terms to bias MOSS transcription (maximum 40)",
    )

    command = commands.add_parser("relabel", help="apply speaker names without retranscribing")
    command.add_argument("input", type=Path)
    command.add_argument("mappings", nargs="+", metavar="SPEAKER=NAME")

    command = commands.add_parser("render", help="render Markdown from saved processing state")
    command.add_argument("input", type=Path)

    command = commands.add_parser("benchmark", help="summarize processed recordings")
    command.add_argument("directory", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "transcribe":
            state = transcribe(
                arguments.input,
                force=arguments.force,
                hotwords=(arguments.hotwords.split(",") if arguments.hotwords is not None else None),
            )
            print(json.dumps({
                "segments": len(state.segments),
                "processing_seconds": state.processing_seconds,
            }))
        elif arguments.command == "relabel":
            state = relabel(arguments.input, arguments.mappings)
            print(json.dumps(state.speakers, sort_keys=True))
        elif arguments.command == "render":
            print(render(arguments.input))
        elif arguments.command == "benchmark":
            print(json.dumps(summarize(arguments.directory), indent=2, sort_keys=True))
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        parser().error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
