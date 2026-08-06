from __future__ import annotations

import re

import yaml

from . import __version__
from .model import Segment, TranscriptState


def timestamp(seconds: float) -> str:
    centiseconds = _centiseconds(seconds)
    total, fraction = divmod(centiseconds, 100)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def timing_offset(seconds: float) -> str:
    return f"{max(0.0, seconds):.2f}"


def _centiseconds(seconds: float) -> int:
    return max(0, round(seconds * 100))


def render_markdown(state: TranscriptState) -> str:
    if re.fullmatch(r"[0-9a-f]{40,64}", __version__) is None:
        raise RuntimeError("speech2md source commit is unavailable")
    title = state.title or "Meeting transcript"
    source_hash = state.source_sha256
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
    seen_attendees: set[str] = set()
    for item in state.attendees:
        handle = item.get("handle", "").strip()
        if not handle or handle in seen_attendees:
            continue
        attendees.append({"handle": handle, "identity": item.get("identity", "").strip()})
        seen_attendees.add(handle)
    frontmatter = {
        "source_sha256": source_hash,
        "speech2md_version": __version__,
        **({"hints_sha256": state.hints_sha256} if state.hints_sha256 else {}),
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
    for run in segment_runs(state.segments):
        first = run[0]
        local_speaker = speaker_handles[first.speaker]
        speaker = state.speaker_names.get(local_speaker, local_speaker)
        visible_start = _centiseconds(first.start) / 100
        run_end = max(
            visible_start + 0.01,
            max(_centiseconds(segment.end) for segment in run) / 100,
        )
        content: list[str] = []
        marked_through = float("-inf")
        for index, segment in enumerate(run):
            content.append(segment.text.strip())
            if (
                index < len(run) - 1
                and _centiseconds(segment.end) / 100 > visible_start + 1e-6
                and _centiseconds(segment.end) / 100 < run_end - 1e-6
                and _centiseconds(segment.end) / 100 > marked_through + 1e-6
            ):
                segment_end = _centiseconds(segment.end) / 100
                content.append(
                    f"<!-- {timing_offset(segment_end - visible_start)}s -->"
                )
                marked_through = segment_end
        content.append(
            f"<!-- {timing_offset(run_end - visible_start)}s -->"
        )
        lines.extend([
            f"**[{timestamp(first.start)}] {speaker}:** {' '.join(content)}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def coalesce_segments(segments: list[Segment], *, max_gap: float = 1.25) -> list[Segment]:
    output: list[Segment] = []
    for run in segment_runs(segments, max_gap=max_gap):
        combined = Segment(**run[0].__dict__)
        for segment in run[1:]:
            combined.end = max(combined.end, segment.end)
            combined.text = f"{combined.text.rstrip()} {segment.text.lstrip()}"
        output.append(combined)
    return output


def segment_runs(
    segments: list[Segment],
    *,
    max_gap: float = 1.25,
) -> list[list[Segment]]:
    output: list[list[Segment]] = []
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        if (
            output
            and output[-1][-1].speaker == segment.speaker
            and output[-1][-1].source_role == segment.source_role
            and segment.start - max(item.end for item in output[-1]) <= max_gap
        ):
            output[-1].append(segment)
        else:
            output.append([segment])
    return output
