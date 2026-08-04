from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .pipeline import relabel, transcribe


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="speech2md",
        description="Transcribe local meeting audio with MOSS and ReDimNet2",
        epilog="Use 'speech2md relabel --help' to assign attendee identities.",
    )
    command.add_argument("--version", action="version", version=f"speech2md {__version__}")
    command.add_argument("input", type=Path)
    command.add_argument("--force", action="store_true", help="replace existing derived output")
    command.add_argument(
        "--hotwords",
        metavar="TERM,...",
        help="comma-separated names and domain terms (maximum 40)",
    )
    return command


def relabel_parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="speech2md relabel",
        description="Assign attendee identities without retranscribing",
    )
    command.add_argument("input", type=Path)
    command.add_argument("mappings", nargs="+", metavar="HANDLE=IDENTITY")
    return command


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        if values[:1] == ["relabel"]:
            arguments = relabel_parser().parse_args(values[1:])
            print(json.dumps(relabel(arguments.input, arguments.mappings), sort_keys=True))
        else:
            arguments = parser().parse_args(values)
            state = transcribe(
                arguments.input,
                force=arguments.force,
                hotwords=(arguments.hotwords.split(",") if arguments.hotwords is not None else None),
            )
            print(json.dumps({
                "segments": len(state.segments),
                "processing_seconds": state.processing_seconds,
            }))
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        parser().error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
