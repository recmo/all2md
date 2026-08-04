from __future__ import annotations

from dataclasses import asdict

import yaml

from .model import Segment, TranscriptState


def timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_markdown(state: TranscriptState) -> str:
    title = state.title or "Meeting transcript"
    frontmatter = {
        "title": title,
        "speech2md": {
            "schema_version": state.schema_version,
            "source": state.source,
            "capture_manifest": state.capture_manifest,
            "meeting_id": state.meeting_id,
            "started_at": state.started_at,
            "created_at": state.created_at,
            "model": {
                "id": state.model,
                "revision": state.model_revision,
            },
            "audio": [asdict(source) for source in state.audio],
            "processing": {
                "duration_seconds": state.processing_seconds,
                "warnings": state.warnings,
            },
            "derived_artifacts": state.derived_artifacts,
        },
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
        speaker = state.speakers.get(segment.speaker, segment.speaker)
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
