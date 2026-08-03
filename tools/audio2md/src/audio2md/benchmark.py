from __future__ import annotations

from pathlib import Path
import json

from .model import TranscriptState
from .moss import MOSS_MODEL


def summarize(directory: Path) -> dict[str, object]:
    states = []
    skipped = 0
    for path in sorted(directory.expanduser().rglob("*.audio2md.json")):
        value = json.loads(path.read_text())
        if value.get("model") != MOSS_MODEL or not value.get("model_revision"):
            skipped += 1
            continue
        states.append(TranscriptState.from_dict(value))
    audio_seconds = sum(source.duration_seconds for state in states for source in state.audio)
    processing_seconds = sum(state.processing_seconds for state in states)
    return {
        "recordings": len(states),
        "audio_seconds": audio_seconds,
        "processing_seconds": processing_seconds,
        "realtime_factor": processing_seconds / audio_seconds if audio_seconds else None,
        "model": MOSS_MODEL if states else None,
        "model_revisions": sorted({state.model_revision for state in states}),
        "skipped_incompatible_states": skipped,
    }
