from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .constants import commit_version
from .pipeline import convert


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="pages2md",
        description="Convert one document or image directory to Markdown",
    )
    command.add_argument("--version", action="version", version=f"pages2md {commit_version()}")
    command.add_argument("input", type=Path)
    command.add_argument("--force", action="store_true", help="replace an existing output")
    return command


def main(argv: list[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    try:
        print(convert(arguments.input, force=arguments.force))
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"pages2md: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
