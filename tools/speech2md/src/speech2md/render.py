from __future__ import annotations

import re

import yaml

from . import __version__
from .model import Segment, TranscriptState


def timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_markdown(state: TranscriptState) -> str:
    if re.fullmatch(r"[0-9a-f]{40,64}", __version__) is None:
        raise RuntimeError("speech2md source commit is unavailable")
    title = state.title or "Meeting transcript"
    source_hash = state.source_sha256 or (state.audio[0].sha256 if state.audio else None)
    if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        raise ValueError("speech2md source hash is unavailable")
    speaker_handles: dict[str, str] = {}
    for segment in sorted(state.segments, key=lambda item: (item.start, item.end, item.source_role)):
        if segment.speaker in speaker_handles:
            continue
        handle = segment.speaker
        if re.fullmatch(r"speaker-\d+", handle) is None or handle in speaker_handles.values():
            number = 1
            while f"speaker-{number}" in speaker_handles.values():
                number += 1
            handle = f"speaker-{number}"
        speaker_handles[segment.speaker] = handle
    attendees = []
    existing = {item.get("handle"): item for item in state.attendees if item.get("handle")}
    for source_speaker, handle in speaker_handles.items():
        attendees.append({
            "handle": handle,
            "identity": state.speakers.get(
                handle,
                state.speakers.get(source_speaker, existing.get(handle, {}).get("identity", "")),
            ),
        })
    attendees.extend(
        {"identity": item.get("identity", "")}
        for item in state.attendees
        if not item.get("handle")
    )
    frontmatter = {
        "source_sha256": source_hash,
        "speech2md_version": __version__,
        **({"started_at": state.started_at} if state.started_at else {}),
        **({"ended_at": state.ended_at} if state.ended_at else {}),
        **({"calendar_event": state.calendar_event} if state.calendar_event else {}),
        "attendees": attendees,
    }
    lines = [
        "---",
        yaml.safe_dump(
            frontmatter,
            allow_unicode=True,
            sort_keys=False,
        ).rstrip(),
        "---",
        "",
        f"# {title}",
        "",
        "## Transcript",
        "",
    ]
    for segment in coalesce_segments(state.segments):
        speaker = speaker_handles[segment.speaker]
        lines.extend([
            f"**[{timestamp(segment.start)}] {speaker}:** {segment.text.strip()}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def coalesce_segments(segments: list[Segment], *, max_gap: float = 1.25) -> list[Segment]:
    output: list[Segment] = []
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        if (
            output
            and output[-1].speaker == segment.speaker
            and output[-1].source_role == segment.source_role
            and segment.start - output[-1].end <= max_gap
        ):
            output[-1].end = max(output[-1].end, segment.end)
            output[-1].text = f"{output[-1].text.rstrip()} {segment.text.lstrip()}"
        else:
            output.append(Segment(**segment.__dict__))
    return output
