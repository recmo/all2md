from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import numpy as np
import yaml


TURN = re.compile(r"^\*\*\[(\d{2}):(\d{2}):(\d{2})\] ([^:]+):\*\*\s?(.*)$")
MEDIA_SUFFIXES = {".aac", ".caf", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}


@dataclass(frozen=True)
class TranscriptFile:
    root: Path
    markdown: Path
    requested: Path | None = None

    @property
    def relative(self) -> Path:
        return (self.requested or self.markdown).relative_to(self.root)

    @property
    def identifier(self) -> str:
        return base64.urlsafe_b64encode(str(self.relative).encode()).decode().rstrip("=")

    @property
    def hint_path(self) -> Path:
        return self.markdown.with_suffix(".hint.yaml")

    @property
    def voiceprints_path(self) -> Path:
        return self.markdown.with_suffix(".voiceprints.npz")

    @property
    def status(self) -> str:
        if not self.markdown.exists():
            return "unprocessed"
        try:
            parsed = parse_markdown(self.markdown)
        except ValueError:
            return "stale"
        _, hint_revision = load_hint_document(self.hint_path)
        rendered_revision = parsed["frontmatter"].get("hints_sha256")
        if rendered_revision != hint_revision and (rendered_revision is not None or hint_revision is not None):
            return "stale"
        return "ready"

    @property
    def stale_reason(self) -> str | None:
        if self.status != "stale":
            return None
        try:
            parse_markdown(self.markdown)
        except ValueError:
            return "schema"
        return "hints"


def discover(root: Path) -> list[TranscriptFile]:
    root = root.expanduser().resolve()
    found: list[TranscriptFile] = []
    claimed_markdown: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        markdown = path.with_suffix(".md")
        found.append(TranscriptFile(root, markdown, path))
        claimed_markdown.add(markdown)
    for path in root.rglob("*-capture.json"):
        markdown = path.with_name(path.name.removesuffix("-capture.json") + ".md")
        found.append(TranscriptFile(root, markdown, path))
        claimed_markdown.add(markdown)
    for path in root.rglob("*.md"):
        if path in claimed_markdown:
            continue
        try:
            parse_markdown(path)
        except ValueError:
            continue
        found.append(TranscriptFile(root, path))
    return sorted(
        found,
        key=lambda item: (item.requested or item.markdown).stat().st_mtime,
        reverse=True,
    )


def resolve_identifier(root: Path, identifier: str) -> TranscriptFile:
    padding = "=" * (-len(identifier) % 4)
    try:
        relative = base64.urlsafe_b64decode(identifier + padding).decode()
    except Exception as error:
        raise ValueError("invalid transcript id") from error
    root = root.expanduser().resolve()
    requested = (root / relative).resolve()
    if requested.parent != root and root not in requested.parents:
        raise ValueError("transcript escapes review folder")
    for item in discover(root):
        if (item.requested or item.markdown) == requested:
            return item
    raise FileNotFoundError(requested)


def parse_markdown(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    try:
        _, raw_frontmatter, body = text.split("---", 2)
        frontmatter = yaml.safe_load(raw_frontmatter) or {}
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError("invalid transcript frontmatter") from error
    if not isinstance(frontmatter, dict) or "speech2md_version" not in frontmatter:
        raise ValueError("not a speech2md transcript")
    title_match = re.search(r"^# (.+)$", body, re.MULTILINE)
    turns = []
    for line in body.splitlines():
        match = TURN.match(line)
        if not match:
            continue
        hours, minutes, seconds, speaker, content = match.groups()
        start = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        turns.append({
            "index": len(turns),
            "start": start,
            "speaker": speaker.strip(),
            "text": content.strip(),
        })
    for index, turn in enumerate(turns):
        turn["end"] = turns[index + 1]["start"] if index + 1 < len(turns) else turn["start"] + 10
    return {
        "title": title_match.group(1).strip() if title_match else path.stem,
        "frontmatter": frontmatter,
        "turns": turns,
    }


def review_progress(parsed: dict[str, Any], hints: dict[str, Any]) -> dict[str, Any]:
    identified_handles = {
        attendee.get("handle", "").strip()
        for attendee in parsed.get("frontmatter", {}).get("attendees", [])
        if isinstance(attendee, dict)
        and isinstance(attendee.get("handle"), str)
        and isinstance(attendee.get("identity"), str)
        and attendee["identity"].strip()
    }
    guided_ranges = [
        value
        for speaker in hints.get("speakers", [])
        if isinstance(speaker, dict)
        for value in speaker.get("ranges", [])
        if isinstance(value, dict)
    ]
    unassigned_runs = 0
    anonymous_speakers = set()
    turns = parsed.get("turns", [])
    for turn in turns:
        if turn["speaker"] in identified_handles:
            continue
        intervals = sorted(
            (
                max(turn["start"], float(value["start"])),
                min(turn["end"], float(value["end"])),
            )
            for value in guided_ranges
            if float(value.get("end", 0)) > turn["start"]
            and float(value.get("start", 0)) < turn["end"]
        )
        cursor = turn["start"]
        turn_unassigned = 0
        for start, end in intervals:
            if start > cursor + 0.01:
                turn_unassigned += 1
            cursor = max(cursor, end)
        if cursor < turn["end"] - 0.01:
            turn_unassigned += 1
        if turn_unassigned:
            anonymous_speakers.add(turn["speaker"])
            unassigned_runs += turn_unassigned
    return {
        "complete": bool(turns) and unassigned_runs == 0,
        "unassignedRunCount": unassigned_runs,
        "unassignedSpeakerCount": len(anonymous_speakers),
    }


def audio_sources(transcript: TranscriptFile) -> list[dict[str, Any]]:
    if transcript.requested and transcript.requested.suffix.lower() in MEDIA_SUFFIXES:
        return [{"role": "mixed", "path": transcript.requested, "name": transcript.requested.name}]
    if transcript.requested and transcript.requested.name.endswith("-capture.json"):
        manifest = transcript.requested
        value = json.loads(manifest.read_text())
        sources = []
        for track in value.get("audio", []):
            path = (manifest.parent / track.get("file", "")).resolve()
            if path.is_file() and (path.parent == transcript.root or transcript.root in path.parents):
                sources.append({"role": track.get("role", "mixed"), "path": path, "name": path.name})
        return sources
    direct = [
        path for path in transcript.markdown.parent.iterdir()
        if path.is_file()
        and path.stem == transcript.markdown.stem
        and path.suffix.lower() in MEDIA_SUFFIXES
    ]
    if direct:
        return [{"role": "mixed", "path": direct[0], "name": direct[0].name}]
    manifest = transcript.markdown.with_name(f"{transcript.markdown.stem}-capture.json")
    if not manifest.is_file():
        return []
    value = json.loads(manifest.read_text())
    sources = []
    for track in value.get("audio", []):
        path = (manifest.parent / track.get("file", "")).resolve()
        if path.is_file() and (path.parent == transcript.root or transcript.root in path.parents):
            sources.append({"role": track.get("role", "mixed"), "path": path, "name": path.name})
    return sources


def load_hint_document(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {
            "title": None,
            "started_at": None,
            "ended_at": None,
            "calendar_event": None,
            "hotwords": [],
            "attendees": [],
            "speakers": [],
            "edits": [],
        }, None
    raw = path.read_bytes()
    value = yaml.safe_load(raw) or {}
    if not isinstance(value, dict):
        raise ValueError("hint sidecar must be a mapping")
    document = {
        "title": value.get("title"),
        "started_at": value.get("started_at"),
        "ended_at": value.get("ended_at"),
        "calendar_event": value.get("calendar_event"),
        "hotwords": value.get("hotwords", []),
        "attendees": value.get("attendees", []),
        "speakers": value.get("speakers", []),
        "edits": value.get("edits", []),
    }
    return document, hashlib.sha256(raw).hexdigest()


def write_hint_document(path: Path, document: dict[str, Any], revision: str | None) -> str:
    validate_hint_document(document)
    _, current_revision = load_hint_document(path)
    if revision != current_revision:
        raise RuntimeError("hint sidecar changed on disk")
    cleaned = {
        key: document.get(key, [])
        for key in (
            "title", "started_at", "ended_at", "calendar_event",
            "hotwords", "attendees", "speakers", "edits",
        )
        if document.get(key)
    }
    raw = yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False).encode()
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(raw)
    temporary.replace(path)
    return hashlib.sha256(raw).hexdigest()


def validate_hint_document(document: dict[str, Any]) -> None:
    allowed = {
        "title", "started_at", "ended_at", "calendar_event",
        "hotwords", "attendees", "speakers", "edits",
    }
    if set(document) - allowed:
        raise ValueError("hint document has unknown fields")
    hotwords = document.get("hotwords", [])
    if not isinstance(hotwords, list) or not all(_line(item) for item in hotwords):
        raise ValueError("hotwords must be a list of non-empty single-line strings")
    for key in ("title", "started_at", "ended_at", "calendar_event"):
        value = document.get(key)
        if value is not None and not _line(value):
            raise ValueError(f"{key} must be a non-empty single-line string")
    if document.get("calendar_event") and not document["calendar_event"].startswith(("https://", "http://")):
        raise ValueError("calendar_event must be an http(s) URL")
    attendees = document.get("attendees", [])
    if not isinstance(attendees, list):
        raise ValueError("attendees must be a list")
    attendee_names = []
    for attendee in attendees:
        if not _line(attendee):
            raise ValueError("each attendee must be a non-empty single-line string")
        attendee_names.append(attendee.strip())
    if len(attendee_names) != len(set(attendee_names)):
        raise ValueError("attendee identities must be unique")
    speakers = document.get("speakers", [])
    if not isinstance(speakers, list):
        raise ValueError("speakers must be a list")
    identities = set()
    for speaker in speakers:
        if not isinstance(speaker, dict) or set(speaker) != {"identity", "ranges"} or not _line(speaker["identity"]):
            raise ValueError("each speaker must contain only identity and ranges")
        if speaker["identity"].strip() in identities:
            raise ValueError("speaker identities must be unique")
        identities.add(speaker["identity"].strip())
        if not isinstance(speaker["ranges"], list) or not speaker["ranges"]:
            raise ValueError("speaker ranges must be a non-empty list")
        for value in speaker["ranges"]:
            _validate_range(value, {"start", "end", "track"}, "speaker range")
    edits = document.get("edits", [])
    if not isinstance(edits, list):
        raise ValueError("edits must be a list")
    for edit in edits:
        _validate_range(edit, {"start", "end", "track", "before", "after"}, "edit")
        if not _line(edit.get("before")) or not _line(edit.get("after")):
            raise ValueError("edit before and after must be non-empty single-line strings")


def _validate_range(value: Any, allowed: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) - allowed or not {"start", "end"} <= set(value):
        raise ValueError(f"invalid {label}")
    start, end = value["start"], value["end"]
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or start < 0 or end <= start:
        raise ValueError(f"invalid {label} interval")
    if "track" in value and not _line(value["track"]):
        raise ValueError(f"invalid {label} track")


def _line(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\n" not in value and "\r" not in value


def transcript_payload(transcript: TranscriptFile) -> dict[str, Any]:
    status = transcript.status
    hints, revision = load_hint_document(transcript.hint_path)
    try:
        parsed = parse_markdown(transcript.markdown)
    except (FileNotFoundError, ValueError):
        parsed = None
    if parsed is None:
        return {
            "id": transcript.identifier,
            "name": str(transcript.relative),
            "title": hints.get("title") or transcript.markdown.stem,
            "status": status,
            "staleReason": transcript.stale_reason,
            "editable": False,
            "frontmatter": {},
            "turns": [],
            "hints": hints,
            "hintRevision": revision,
            "audio": [
                {"index": index, "role": source["role"], "name": source["name"]}
                for index, source in enumerate(audio_sources(transcript))
            ],
        }
    sources = audio_sources(transcript)
    return {
        "id": transcript.identifier,
        "name": str(transcript.relative),
        "title": parsed["title"],
        "status": status,
        "staleReason": transcript.stale_reason,
        "editable": True,
        "frontmatter": parsed["frontmatter"],
        "turns": parsed["turns"],
        "hints": hints,
        "hintRevision": revision,
        "audio": [
            {"index": index, "role": source["role"], "name": source["name"]}
            for index, source in enumerate(sources)
        ],
    }


def candidate_identities(root: Path, selected: TranscriptFile, handle: str) -> list[dict[str, Any]]:
    selected_vector = _voiceprint(selected, handle)
    if selected_vector is None:
        return []
    candidates: dict[str, tuple[float, str]] = {}
    for transcript in discover(root):
        if transcript.status != "ready":
            continue
        parsed = parse_markdown(transcript.markdown)
        for attendee in parsed["frontmatter"].get("attendees", []):
            if not isinstance(attendee, dict):
                continue
            raw_identity = attendee.get("identity")
            raw_handle = attendee.get("handle")
            identity = raw_identity.strip() if isinstance(raw_identity, str) else ""
            other_handle = raw_handle.strip() if isinstance(raw_handle, str) else ""
            if not identity or not other_handle:
                continue
            vector = _voiceprint(transcript, other_handle)
            if vector is None:
                continue
            denominator = float(np.linalg.norm(selected_vector) * np.linalg.norm(vector))
            similarity = float(np.dot(selected_vector, vector) / denominator) if denominator else 0.0
            previous = candidates.get(identity)
            if previous is None or similarity > previous[0]:
                candidates[identity] = (similarity, str(transcript.relative))
    return [
        {"identity": identity, "similarity": similarity, "source": source}
        for identity, (similarity, source) in sorted(
            candidates.items(), key=lambda item: item[1][0], reverse=True
        )
    ]


def _voiceprint(transcript: TranscriptFile, handle: str):
    if not transcript.voiceprints_path.is_file():
        return None
    try:
        with np.load(transcript.voiceprints_path, allow_pickle=False) as value:
            handles = [str(item) for item in value["handles"]]
            index = handles.index(handle)
            return np.asarray(value["embeddings"][index], dtype=float)
    except (KeyError, ValueError, OSError):
        return None
