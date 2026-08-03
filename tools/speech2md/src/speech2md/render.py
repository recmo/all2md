from __future__ import annotations

from .model import Segment, TranscriptState


def timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_markdown(state: TranscriptState) -> str:
    title = state.title or "Meeting transcript"
    lines = [
        f"# {title}",
        "",
        "## Capture",
        "",
        f"- Source: `{state.source}`",
        f"- Started: {state.started_at or 'unknown'}",
        f"- Model: `{state.model}@{state.model_revision}`",
        f"- Audio SHA-256: `{state.audio[0].sha256}`",
        "",
        "## Transcript",
        "",
    ]
    for segment in coalesce_segments(state.segments):
        speaker = state.speakers.get(segment.speaker, segment.speaker)
        lines.extend([
            f"**[{timestamp(segment.start)}] {speaker}:** {segment.text.strip()}",
            "",
        ])
    if state.warnings:
        lines.extend(["## Processing notes", "", *[f"- {warning}" for warning in state.warnings], ""])
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
