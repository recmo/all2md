from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import json
import re
import tempfile
import time

import yaml
from tqdm.auto import tqdm

from . import __version__
from .media import LEGACY_STATE_SUFFIXES, STATE_SUFFIX, probe, resolve_input, sha256
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
    write_voiceprints,
)
from .render import render_markdown


def transcribe(
    requested: Path,
    *,
    force: bool = False,
    hotwords: list[str] | None = None,
) -> TranscriptState:
    hotwords = normalize_hotwords(hotwords)
    if not re.fullmatch(r"[0-9a-f]{40,64}", __version__):
        raise RuntimeError("speech2md source commit is unavailable")
    prompt = build_transcription_prompt(hotwords)
    resolved = resolve_input(requested)
    source_hash = sha256(resolved.requested)
    voiceprints_path = resolved.markdown_path.with_suffix(".voiceprints.npz")
    raw_path = resolved.state_path.with_name(
        resolved.state_path.name.removesuffix(STATE_SUFFIX) + ".moss.json"
    )
    legacy_paths = [
        resolved.state_path,
        raw_path,
        *[
            resolved.state_path.with_name(
                resolved.state_path.name.removesuffix(STATE_SUFFIX) + suffix
            )
            for suffix in LEGACY_STATE_SUFFIXES
        ],
    ]
    outputs = [resolved.markdown_path, voiceprints_path]
    existing = [path for path in [*outputs, *legacy_paths] if path.exists()]
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

    with tqdm(
        total=sum(source.duration_seconds for _, _, source in sources),
        desc=resolved.requested.name,
        unit="audio-sec",
        dynamic_ncols=True,
        smoothing=0.1,
        disable=None,
    ) as progress:
        progress.set_postfix_str("loading speaker model")
        embedder = get_redimnet2_embedder()
        progress.set_postfix_str("loading transcription model")
        engine = load_moss_engine()
        for path, role, source in sources:

            def report_progress(
                window: int,
                window_count: int,
                attempt: int,
                completed_seconds: float,
                *,
                track_role: str = role,
            ) -> None:
                status = f"{track_role} window {window}/{window_count}"
                if attempt > 1:
                    status += f" recovery {attempt - 1}"
                progress.set_postfix_str(status)
                if completed_seconds:
                    progress.update(completed_seconds)

            track_segments, raw, speaker_profiles = transcribe_track(
                path,
                engine=engine,
                prompt=prompt,
                role=role,
                duration=source.duration_seconds,
                embedder=embedder,
                speaker_profiles=speaker_profiles,
                progress_callback=report_progress,
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
    speaker_profiles = _canonicalize_speakers(segments, speaker_profiles)
    state = TranscriptState(
        schema_version=2,
        source=str(resolved.requested),
        capture_manifest=str(resolved.capture_manifest) if resolved.capture_manifest else None,
        meeting_id=resolved.meeting_id,
        title=resolved.title,
        started_at=resolved.started_at,
        ended_at=getattr(resolved, "ended_at", None),
        calendar_event=getattr(resolved, "calendar_event", None),
        source_sha256=source_hash,
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
        derived_artifacts=[],
    )
    with tempfile.TemporaryDirectory(
        prefix=f".{resolved.markdown_path.stem}-speech2md-",
        dir=resolved.markdown_path.parent,
    ) as temporary:
        staged = Path(temporary)
        staged_markdown = staged / resolved.markdown_path.name
        staged_voiceprints = staged / voiceprints_path.name
        write_voiceprints(speaker_profiles, staged_voiceprints)
        write_text(render_markdown(state), staged_markdown)
        staged_voiceprints.replace(voiceprints_path)
        staged_markdown.replace(resolved.markdown_path)
    for path in legacy_paths:
        path.unlink(missing_ok=True)
    return state


def _canonicalize_speakers(segments, profiles):
    mapping: dict[str, str] = {}
    for segment in sorted(segments, key=lambda item: (item.start, item.end, item.source_role)):
        mapping.setdefault(segment.speaker, f"speaker-{len(mapping) + 1}")
        segment.speaker = mapping[segment.speaker]
    canonical = {}
    for speaker, profile in profiles.items():
        handle = mapping.get(speaker)
        if handle is None:
            continue
        profile.speaker = handle
        canonical[handle] = profile
    return canonical


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
    for suffix in (STATE_SUFFIX, *LEGACY_STATE_SUFFIXES):
        if requested.name.endswith(suffix):
            markdown = requested.with_name(requested.name.removesuffix(suffix) + ".md")
            return TranscriptState.from_dict(json.loads(requested.read_text())), requested, markdown
    resolved = resolve_input(requested)
    state_path = resolved.state_path
    if not state_path.exists():
        for legacy_suffix in reversed(LEGACY_STATE_SUFFIXES):
            legacy_path = state_path.with_name(
                state_path.name.removesuffix(STATE_SUFFIX) + legacy_suffix
            )
            if legacy_path.exists():
                state_path = legacy_path
                break
    return (
        TranscriptState.from_dict(json.loads(state_path.read_text())),
        state_path,
        resolved.markdown_path,
    )


def relabel(requested: Path, mappings: list[str]) -> dict[str, str]:
    markdown_path = _markdown_path(requested)
    if markdown_path.exists():
        metadata, body = _read_markdown(markdown_path)
        attendees = metadata.get("attendees")
        if not isinstance(attendees, list):
            raise ValueError("Markdown front matter lacks attendees")
        identities = {}
        for mapping in mappings:
            if "=" not in mapping:
                raise ValueError(f"expected HANDLE=IDENTITY, got {mapping!r}")
            handle, identity = (part.strip() for part in mapping.split("=", 1))
            attendee = next(
                (item for item in attendees if isinstance(item, dict) and item.get("handle") == handle),
                None,
            )
            if attendee is None:
                raise ValueError(f"unknown attendee handle: {handle}")
            attendee["identity"] = identity
            identities[handle] = identity
        write_text(_write_markdown(metadata, body), markdown_path)
        return identities

    state, _, markdown_path = state_for(requested)
    write_text(render_markdown(state), markdown_path)
    return relabel(markdown_path, mappings)


def render(requested: Path) -> Path:
    markdown_path = _markdown_path(requested)
    if markdown_path.exists():
        return markdown_path
    state, _, markdown_path = state_for(requested)
    write_text(render_markdown(state), markdown_path)
    return markdown_path


def _markdown_path(requested: Path) -> Path:
    requested = requested.expanduser().resolve()
    if requested.suffix.lower() == ".md":
        return requested
    if requested.name.endswith((STATE_SUFFIX, *LEGACY_STATE_SUFFIXES)):
        for suffix in (STATE_SUFFIX, *LEGACY_STATE_SUFFIXES):
            if requested.name.endswith(suffix):
                return requested.with_name(requested.name.removesuffix(suffix) + ".md")
    return resolve_input(requested).markdown_path


def _read_markdown(path: Path) -> tuple[dict, str]:
    value = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", value, re.DOTALL)
    if match is None:
        raise ValueError("Markdown lacks YAML front matter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError("Markdown front matter is not a mapping")
    return metadata, match.group(2)


def _write_markdown(metadata: dict, body: str) -> str:
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{frontmatter}\n---\n{body}"
