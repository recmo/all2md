from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import detect_kind
from .constants import DEFAULT_DPI, MODEL_ID, MODEL_REVISION
from .ocr import MlxUnlimitedOcr, SidecarOcr
from .pipeline import convert
from .util import sha256_file
from .verify import verify_bundle


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ebook2md", description="Convert ebooks and documents to auditable Markdown")
    root.add_argument("--version", action="version", version="ebook2md 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)

    convert_parser = commands.add_parser("convert", help="convert one or more inputs")
    convert_parser.add_argument("inputs", nargs="+", type=Path)
    convert_parser.add_argument("--output", "-o", required=True, type=Path)
    convert_parser.add_argument("--pages")
    convert_parser.add_argument("--language", action="append", default=[])
    convert_parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    convert_parser.add_argument("--split", choices=("auto", "single", "chapters"), default="auto")
    convert_parser.add_argument("--chapter-map", type=Path)
    convert_parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    convert_parser.add_argument(
        "--multi-page",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="analyze adjacent fixed-layout pages together (default: enabled)",
    )
    convert_parser.add_argument("--force", action="store_true")
    convert_parser.add_argument("--json", action="store_true")
    convert_parser.add_argument("--sidecar-ocr", action="store_true", help=argparse.SUPPRESS)

    inspect_parser = commands.add_parser("inspect", help="inspect input without running OCR")
    inspect_parser.add_argument("input", type=Path)
    inspect_parser.add_argument("--json", action="store_true")

    model_parser = commands.add_parser("models", help="manage model weights")
    model_commands = model_parser.add_subparsers(dest="model_command", required=True)
    model_commands.add_parser("fetch", help="download pinned model weights")

    verify_parser = commands.add_parser("verify", help="verify an output bundle")
    verify_parser.add_argument("bundle", type=Path)
    verify_parser.add_argument("--json", action="store_true")
    return root


def main(argv: list[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "convert":
            backend = SidecarOcr() if arguments.sidecar_ocr else MlxUnlimitedOcr()
            outputs = [
                convert(
                    source,
                    arguments.output,
                    dpi=arguments.dpi,
                    pages=arguments.pages,
                    split_mode=arguments.split,
                    chapter_map=arguments.chapter_map,
                    languages=arguments.language,
                    resume=arguments.resume,
                    multi_page=arguments.multi_page,
                    force=arguments.force,
                    backend=backend,
                )
                for source in arguments.inputs
            ]
            if arguments.json:
                print(json.dumps({"outputs": [str(path) for path in outputs]}, indent=2))
            else:
                for path in outputs:
                    print(path)
        elif arguments.command == "inspect":
            source = arguments.input.resolve()
            information = {
                "path": str(source),
                "kind": detect_kind(source),
                "sha256": sha256_file(source) if source.is_file() else None,
            }
            print(json.dumps(information, indent=2) if arguments.json else "\n".join(f"{key}: {value}" for key, value in information.items()))
        elif arguments.command == "models":
            from huggingface_hub import snapshot_download
            path = snapshot_download(MODEL_ID, revision=MODEL_REVISION)
            print(path)
        elif arguments.command == "verify":
            result = verify_bundle(arguments.bundle)
            value = {
                "ok": result.ok,
                "errors": result.errors,
                "warnings": result.warnings,
                "markdown_files": result.markdown_files,
                "assets": result.assets,
            }
            print(json.dumps(value, indent=2) if arguments.json else _format_verification(value))
            if not result.ok:
                raise SystemExit(1)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"ebook2md: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def _format_verification(value: dict) -> str:
    lines = ["OK" if value["ok"] else "FAILED", f"Markdown files: {value['markdown_files']}", f"Assets: {value['assets']}"]
    lines.extend(f"error: {item}" for item in value["errors"])
    lines.extend(f"warning: {item}" for item in value["warnings"])
    return "\n".join(lines)


if __name__ == "__main__":
    main()
