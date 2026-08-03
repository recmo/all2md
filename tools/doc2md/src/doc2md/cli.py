from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .core import DEFAULT_OUTPUT_ROOT, Doc2mdError, GitCheckout, Provider, Repository
from .google_drive import GoogleDriveClient
from .notion import NotionClient


DEFAULT_CONFIG = Path("doc2md.json")
SOURCE_NAMES = ("notion", "google-docs")


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        if args.command == "sync":
            result = run_sync(args)
        elif args.command == "doctor":
            result = run_doctor(args)
        elif args.command == "status":
            result = run_status(args)
        else:
            parser.error("missing command")
            return
    except (Doc2mdError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"doc2md: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "doctor" and not result["ok"]:
        raise SystemExit(1)


def run_sync(args: argparse.Namespace) -> dict[str, Any]:
    config = _load_config(args.config)
    selected = args.source or list(SOURCE_NAMES)
    output_root = _output_root(config)
    with _destination_context(
        args.directory,
        config,
        output_root=output_root,
        dry_run=args.dry_run,
    ) as (directory, checkout):
        repository = Repository(directory, output_root=output_root)
        results: dict[str, Any] = {}
        for source in selected:
            provider = _provider(source, config, output_root)
            if provider is None:
                results[source] = {"disabled": True}
                continue
            results[source] = repository.apply(
                source,
                provider.documents(),
                allow_bulk_delete=args.allow_bulk_delete,
            )

        pushed = False
        if checkout and not args.dry_run:
            sources = ", ".join(selected)
            pushed = checkout.commit_and_push(
                f"doc2md: sync {sources} {datetime.now(UTC).date().isoformat()}"
            )
        results["git"] = {
            "configured": checkout is not None,
            "dry_run": args.dry_run,
            "pushed": pushed,
        }
        return results


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    config = _load_config(args.config)
    issues: list[str] = []
    try:
        _output_root(config)
    except Doc2mdError as exc:
        issues.append(str(exc))

    git = config.get("git", {})
    if git and not isinstance(git, dict):
        issues.append("git must be an object")
    elif isinstance(git, dict):
        if git.get("auth_header") and not git.get("remote"):
            issues.append("git.remote is required when git.auth_header is configured")
        try:
            if "remote" in git:
                _nonempty_string(git.get("remote"), "git.remote")
            if "branch" in git:
                _nonempty_string(git.get("branch"), "git.branch")
            if "author_name" in git:
                _nonempty_string(git.get("author_name"), "git.author_name")
            if "author_email" in git:
                _nonempty_string(git.get("author_email"), "git.author_email")
            if git.get("auth_header"):
                _secret(git.get("auth_header"), "git.auth_header")
                remote = _nonempty_string(git.get("remote"), "git.remote")
                parsed = urlparse(remote)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise Doc2mdError("git.auth_header requires an HTTP or HTTPS remote")
        except Doc2mdError as exc:
            issues.append(str(exc))
    if args.directory is not None and not args.directory.is_dir():
        issues.append(f"output directory does not exist: {args.directory}")
    if args.directory is None and (not isinstance(git, dict) or not git.get("remote")):
        issues.append("provide --directory or configure git.remote")

    notion = config.get("notion", {})
    if not isinstance(notion, dict):
        issues.append("notion must be an object")
    else:
        try:
            notion_enabled = _enabled(notion, "notion")
            _base_url(notion.get("base_url"), "notion.base_url", "https://api.notion.com/v1")
            _path_prefix(notion.get("path_prefix", []), "notion.path_prefix")
            if notion_enabled:
                _secret(notion.get("token"), "notion.token")
        except Doc2mdError as exc:
            issues.append(str(exc))

    google = config.get("google_docs", {})
    if not isinstance(google, dict):
        issues.append("google_docs must be an object")
    else:
        try:
            google_enabled = _enabled(google, "google_docs")
            _base_url(
                google.get("base_url"),
                "google_docs.base_url",
                "https://www.googleapis.com/drive/v3",
            )
            _path_prefix(google.get("path_prefix", []), "google_docs.path_prefix")
            if google_enabled:
                _secret(google.get("bearer"), "google_docs.bearer")
                _root_ids(google.get("root_ids"))
        except Doc2mdError as exc:
            issues.append(str(exc))

    if not any(
        isinstance(config.get(key), dict) and config[key].get("enabled") is True
        for key in ("notion", "google_docs")
    ):
        issues.append("at least one provider must be enabled")
    return {"ok": not issues, "issues": issues}


def run_status(args: argparse.Namespace) -> dict[str, Any]:
    config = _load_config(args.config)
    output_root = _output_root(config)
    with _destination_context(args.directory, config, output_root=output_root) as (
        directory,
        _checkout,
    ):
        manifest = Repository(directory, output_root=output_root).manifest
        return {
            "version": manifest.get("version"),
            "updated_at": manifest.get("updated_at"),
            "sources": {
                source: {"documents": len(entries)}
                for source, entries in manifest.get("sources", {}).items()
            },
        }


def _provider(source: str, config: dict[str, Any], output_root: Path) -> Provider | None:
    if source == "notion":
        notion = _object(config.get("notion", {}), "notion")
        if not _enabled(notion, "notion"):
            return None
        return NotionClient(
            _secret(notion.get("token"), "notion.token"),
            base_url=_base_url(
                notion.get("base_url"),
                "notion.base_url",
                "https://api.notion.com/v1",
            ),
            output_root=output_root,
            path_prefix=_path_prefix(notion.get("path_prefix", []), "notion.path_prefix"),
        )
    if source == "google-docs":
        google = _object(config.get("google_docs", {}), "google_docs")
        if not _enabled(google, "google_docs"):
            return None
        roots = _root_ids(google.get("root_ids"))
        return GoogleDriveClient(
            _secret(google.get("bearer"), "google_docs.bearer"),
            roots,
            base_url=_base_url(
                google.get("base_url"),
                "google_docs.base_url",
                "https://www.googleapis.com/drive/v3",
            ),
            path_prefix=_path_prefix(google.get("path_prefix", []), "google_docs.path_prefix"),
        )
    raise Doc2mdError(f"unsupported source: {source}")


@contextmanager
def _destination_context(
    directory: Path | None,
    config: dict[str, Any],
    *,
    output_root: Path,
    dry_run: bool = False,
) -> Iterator[tuple[Path, GitCheckout | None]]:
    if directory:
        directory = directory.resolve()
        if not directory.is_dir():
            raise Doc2mdError(f"output directory does not exist: {directory}")
        if dry_run:
            with tempfile.TemporaryDirectory(prefix="doc2md-preview-") as tmp:
                preview = Path(tmp) / "output"
                shutil.copytree(directory, preview)
                yield preview, None
            return
        yield directory, None
        return

    git = _object(config.get("git", {}), "git")
    remote = git.get("remote")
    if not isinstance(remote, str) or not remote:
        raise Doc2mdError("provide --directory or configure git.remote")
    checkout = GitCheckout(
        _nonempty_string(remote, "git.remote"),
        _nonempty_string(git.get("branch", "main"), "git.branch"),
        auth_header=_optional_secret(git.get("auth_header"), "git.auth_header"),
        author_name=_nonempty_string(git.get("author_name", "doc2md"), "git.author_name"),
        author_email=_nonempty_string(
            git.get("author_email", "doc2md@localhost"),
            "git.author_email",
        ),
        output_root=output_root,
    )
    with checkout as path:
        yield path, checkout


def _load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise Doc2mdError("configuration must be a JSON object")
    return data


def _output_root(config: dict[str, Any]) -> Path:
    output = _object(config.get("output", {}), "output")
    value = output.get("root", DEFAULT_OUTPUT_ROOT.as_posix())
    if not isinstance(value, str) or not value:
        raise Doc2mdError("output.root must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise Doc2mdError("output.root must be a safe relative path")
    return path


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Doc2mdError(f"{field} must be an object")
    return value


def _path_prefix(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(part, str) and part for part in value):
        raise Doc2mdError(f"{field} must be a list of non-empty strings")
    return tuple(value)


def _enabled(config: dict[str, Any], field: str) -> bool:
    value = config.get("enabled", False)
    if not isinstance(value, bool):
        raise Doc2mdError(f"{field}.enabled must be a boolean")
    return value


def _base_url(value: Any, field: str, default: str) -> str:
    resolved = default if value is None else _nonempty_string(value, field)
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise Doc2mdError(f"{field} must be an HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise Doc2mdError(f"{field} must not contain credentials")
    return resolved.rstrip("/")


def _root_ids(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(root, str) and root.strip() for root in value)
    ):
        raise Doc2mdError("google_docs.root_ids must be a non-empty list of strings")
    return value


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Doc2mdError(f"{field} must be a non-empty string")
    return value


def _secret(value: Any, field: str) -> str:
    resolved = _optional_secret(value, field)
    if not resolved:
        raise Doc2mdError(f"{field} is required")
    return resolved


def _optional_secret(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"env"} and isinstance(value["env"], str):
        name = value["env"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise Doc2mdError(f"{field} env must be a valid environment variable name")
        resolved = os.environ.get(name, "")
        if not resolved:
            raise Doc2mdError(f"environment variable {name} for {field} is not set")
        return resolved
    raise Doc2mdError(f"{field} must be a string or an object containing only env")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract durable Markdown from document providers")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync")
    sync.add_argument("--source", action="append", choices=SOURCE_NAMES)
    _destination_arguments(sync)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--directory", type=Path)

    status = subparsers.add_parser("status")
    status.add_argument("--directory", type=Path)

    return parser


def _destination_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-bulk-delete", action="store_true")


if __name__ == "__main__":
    main()
