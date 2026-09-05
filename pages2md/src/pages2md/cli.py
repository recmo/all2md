from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .constants import commit_version
from .pipeline import convert


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="pages2md",
        description="Convert one or more documents or image directories to Markdown",
    )
    command.add_argument("--version", action="version", version=f"pages2md {commit_version()}")
    command.add_argument("input", nargs="+", type=Path)
    command.add_argument("--force", action="store_true", help="replace an existing output")
    command.add_argument(
        "--ignore-embedded-text",
        action="store_true",
        help="ignore embedded PDF/DjVu text and use visual OCR only",
    )
    return command


def main(argv: list[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    failed = False
    for source in arguments.input:
        try:
            print(
                convert(
                    source,
                    force=arguments.force,
                    ignore_embedded_text=arguments.ignore_embedded_text,
                )
            )
        except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
            failed = True
            print(f"pages2md: {error}", file=sys.stderr)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
