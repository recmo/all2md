from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import json
import time

from .media import probe, resolve_input
from .model import TranscriptState
from .moss import (
    MAX_GENERATION_TOKENS,
    MAX_RECOVERY_ATTEMPTS,
    MIN_RECOVERY_PROGRESS_SECONDS,
    MOSS_MODEL,
    MOSS_REVISION,
    RECOVERY_OVERLAP_SECONDS,
    RECOVERY_TOKEN_THRESHOLD,
    SILENCE_MIN_SECONDS,
    SILENCE_NOISE_DB,
    SILENCE_SEARCH_SECONDS,
    TARGET_PART_SECONDS,
    WINDOW_OVERLAP_SECONDS,
    build_transcription_prompt,
    load_moss_engine,
    normalize_hotwords,
    transcribe_track,
)
from .redimnet2 import (
    DEFAULT_SIMILARITY_MARGIN,
    DEFAULT_SIMILARITY_THRESHOLD,
    REDIMNET2_CHECKPOINT_SHA256,
    REDIMNET2_DIMENSION,
    REDIMNET2_MODEL,
    REDIMNET2_REVISION,
    get_redimnet2_embedder,
    profile_diagnostics,
)
from .render import render_markdown


def transcribe(
    requested: Path,
    *,
    force: bool = False,
    hotwords: list[str] | None = None,
) -> TranscriptState:
    hotwords = normalize_hotwords(hotwords)
    prompt = build_transcription_prompt(hotwords)
    resolved = resolve_input(requested)
    raw_path = resolved.state_path.with_name(
        resolved.state_path.name.removesuffix(".audio2md.json") + ".moss.json"
    )
    outputs = [resolved.state_path, resolved.markdown_path, raw_path]
    existing = [path for path in outputs if path.exists()]
    if existing and not force:
        raise FileExistsError("derived artifact exists (use --force): " + ", ".join(str(path) for path in existing))

    started = time.monotonic()
    audio = []
    segments = []
    raw_tracks = []
    speaker_profiles = {}
    sources = []
    for path, role, expected_checksum in resolved.sources:
        source = probe(path, expected_sha256=expected_checksum, role=role)
        audio.append(source)
        sources.append((path, role, source))

    embedder = get_redimnet2_embedder()
    engine = load_moss_engine()
    for path, role, source in sources:
        track_segments, raw, speaker_profiles = transcribe_track(
            path,
            engine=engine,
            prompt=prompt,
            role=role,
            duration=source.duration_seconds,
            embedder=None if role == "microphone" else embedder,
            speaker_profiles=speaker_profiles,
        )
        segments.extend(track_segments)
        raw_tracks.append(raw)

    window_count = sum(len(track["windows"]) for track in raw_tracks)
    actual_overlap = max((track["actual_overlap_seconds"] for track in raw_tracks), default=0.0)
    warnings = (
        [
            "MOSS processed the recording in one pass and provides meeting-global anonymous speaker labels",
            "ReDimNet2 vectors are retained as meeting-local evidence but were not needed to stitch MOSS windows",
        ]
        if window_count == len(raw_tracks)
        else [
            "MOSS provides window-local diarization; speaker continuity across windows uses ReDimNet2 voice embeddings",
            (
                "audio overlap is used only for transcript boundary trimming and deduplication"
                if actual_overlap
                else "automatic equal MOSS parts are non-overlapping; transcript boundaries are not duplicated"
            ),
            "unmatched speakers receive new anonymous labels and may be relabeled later",
        ]
    )
    warnings.extend(
        warning
        for track in raw_tracks
        for warning in track.get("warnings", [])
    )
    state = TranscriptState(
        schema_version=2,
        source=str(resolved.requested),
        capture_manifest=str(resolved.capture_manifest) if resolved.capture_manifest else None,
        meeting_id=resolved.meeting_id,
        title=resolved.title,
        started_at=resolved.started_at,
        model=MOSS_MODEL,
        model_revision=MOSS_REVISION,
        created_at=datetime.now(UTC).isoformat(),
        processing_seconds=time.monotonic() - started,
        audio=audio,
        speakers={},
        segments=sorted(segments, key=lambda item: (item.start, item.end, item.source_role)),
        warnings=warnings,
        speaker_profiles=speaker_profiles,
        provenance={
            "window_strategy": "equal-silence-aligned",
            "target_part_seconds": TARGET_PART_SECONDS,
            "actual_overlap_seconds": actual_overlap,
            "window_overlap_seconds": WINDOW_OVERLAP_SECONDS,
            "silence_search_seconds": SILENCE_SEARCH_SECONDS,
            "silence_noise_db": SILENCE_NOISE_DB,
            "silence_min_seconds": SILENCE_MIN_SECONDS,
            "max_generation_tokens": MAX_GENERATION_TOKENS,
            "recovery_overlap_seconds": RECOVERY_OVERLAP_SECONDS,
            "recovery_token_threshold": RECOVERY_TOKEN_THRESHOLD,
            "minimum_recovery_progress_seconds": MIN_RECOVERY_PROGRESS_SECONDS,
            "max_recovery_attempts": MAX_RECOVERY_ATTEMPTS,
            "transcription_prompt": prompt,
            "hotwords": hotwords,
            "speaker_reconciliation": {
                "method": "ReDimNet2 cosine similarity",
                "model": REDIMNET2_MODEL,
                "model_revision": REDIMNET2_REVISION,
                "checkpoint_sha256": REDIMNET2_CHECKPOINT_SHA256,
                "embedding_dimension": REDIMNET2_DIMENSION,
                "similarity_threshold": DEFAULT_SIMILARITY_THRESHOLD,
                "similarity_margin": DEFAULT_SIMILARITY_MARGIN,
                "assignment": "one-to-one within each MOSS window",
                "text_used_for_identity": False,
            },
        },
        derived_artifacts=[str(raw_path)],
    )
    write_json({
        "schema_version": "audio2md-moss-raw-v2",
        "model": MOSS_MODEL,
        "model_revision": MOSS_REVISION,
        "transcription_prompt": prompt,
        "hotwords": hotwords,
        "tracks": raw_tracks,
        "speaker_profiles": profile_diagnostics(speaker_profiles),
    }, raw_path)
    write_state(state, resolved.state_path)
    write_text(render_markdown(state), resolved.markdown_path)
    return state


def write_json(value: object, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    temporary.replace(path)


def write_state(state: TranscriptState, path: Path) -> None:
    write_json(state.to_dict(), path)


def write_text(value: str, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(value)
    temporary.replace(path)


def state_for(requested: Path) -> tuple[TranscriptState, Path, Path]:
    requested = requested.expanduser().resolve()
    if requested.name.endswith(".audio2md.json"):
        markdown = requested.with_name(requested.name.removesuffix(".audio2md.json") + ".md")
        return TranscriptState.from_dict(json.loads(requested.read_text())), requested, markdown
    resolved = resolve_input(requested)
    return (
        TranscriptState.from_dict(json.loads(resolved.state_path.read_text())),
        resolved.state_path,
        resolved.markdown_path,
    )


def relabel(requested: Path, mappings: list[str]) -> TranscriptState:
    state, state_path, markdown_path = state_for(requested)
    for mapping in mappings:
        if "=" not in mapping:
            raise ValueError(f"expected SPEAKER=NAME, got {mapping!r}")
        speaker, name = mapping.split("=", 1)
        if not speaker.strip() or not name.strip():
            raise ValueError(f"expected SPEAKER=NAME, got {mapping!r}")
        state.speakers[speaker.strip()] = name.strip()
    write_state(state, state_path)
    write_text(render_markdown(state), markdown_path)
    return state


def render(requested: Path) -> Path:
    state, _, markdown_path = state_for(requested)
    write_text(render_markdown(state), markdown_path)
    return markdown_path
