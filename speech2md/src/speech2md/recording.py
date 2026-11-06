"""Authored recording documents consumed by standalone and worker execution."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

import yaml

from .media import resolve_input
from .model import ResolvedInput


def frontmatter(path: Path) -> dict:
    if path.suffix.lower() != ".md":
        raise ValueError("speech2md input must be a recording Markdown document")
    text = path.read_bytes().decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        raise ValueError("recording requires YAML frontmatter")
    try:
        value = yaml.safe_load(match[1])
    except yaml.YAMLError as error:
        raise ValueError(f"invalid recording YAML: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("recording frontmatter must be a mapping")
    return value


def resolve_recording(path: Path, metadata: dict) -> ResolvedInput:
    path = path.expanduser().resolve()
    sources = [metadata[key] for key in ("audio", "manifest") if key in metadata]
    if len(sources) != 1 or not isinstance(sources[0], str):
        raise ValueError("recording requires exactly one audio or manifest path")
    reference = Path(sources[0])
    if reference.is_absolute() or ".." in reference.parts:
        raise ValueError("recording source must be relative and inside its directory")
    resolved = resolve_input(path.parent / reference)
    output = path.with_name("transcript.md") if path.name == "recording.md" else path.with_suffix(".transcript.md")
    return replace(resolved, requested=path, markdown_path=output)
