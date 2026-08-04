from __future__ import annotations

from pathlib import Path
import re
import tempfile
import time

from tqdm.auto import tqdm

from . import __version__
from .hints import hint_path, load_hints, validate_hints
from .media import probe, resolve_input, sha256
from .model import TranscriptState
from .moss import (
    build_transcription_prompt,
    load_moss_engine,
    transcribe_track,
)
from .redimnet2 import (
    get_redimnet2_embedder,
    write_voiceprints,
)
from .render import render_markdown


def transcribe(
    requested: Path,
    *,
    force: bool = False,
) -> TranscriptState:
    if not re.fullmatch(r"[0-9a-f]{40,64}", __version__):
        raise RuntimeError("speech2md source commit is unavailable")
    resolved = resolve_input(requested)
    hints = load_hints(hint_path(resolved))
    source_hash = sha256(resolved.requested)
    voiceprints_path = resolved.markdown_path.with_suffix(".voiceprints.npz")
    outputs = [resolved.markdown_path, voiceprints_path]
    existing = [path for path in outputs if path.exists()]
    if existing and not force:
        raise FileExistsError("derived artifact exists (use --force): " + ", ".join(str(path) for path in existing))

    started = time.monotonic()
    segments = []
    speaker_profiles = {}
    sources = []
    for path, role, expected_checksum in resolved.sources:
        source = probe(path, expected_sha256=expected_checksum, role=role)
        sources.append((path, role, source))
    hints = validate_hints(hints, [source for _, _, source in sources])
    prompt = build_transcription_prompt(list(hints.hotwords))
    hinted_tracks = {hint.track for hint in hints.speakers}
    if hinted_tracks:
        sources.sort(key=lambda item: item[1] not in hinted_tracks)

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

            track_segments, _, speaker_profiles = transcribe_track(
                path,
                engine=engine,
                prompt=prompt,
                role=role,
                duration=source.duration_seconds,
                embedder=embedder,
                speaker_profiles=speaker_profiles,
                speaker_hints=hints.speakers,
                progress_callback=report_progress,
            )
            segments.extend(track_segments)
    speaker_profiles, identities = _canonicalize_speakers(segments, speaker_profiles)
    state = TranscriptState(
        title=resolved.title,
        started_at=resolved.started_at,
        ended_at=getattr(resolved, "ended_at", None),
        calendar_event=getattr(resolved, "calendar_event", None),
        source_sha256=source_hash,
        processing_seconds=time.monotonic() - started,
        segments=sorted(segments, key=lambda item: (item.start, item.end, item.source_role)),
        attendees=[
            {"handle": handle, "identity": identity}
            for handle, identity in identities.items()
        ],
        hints_sha256=hints.sha256,
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
    return state


def _canonicalize_speakers(segments, profiles):
    mapping: dict[str, str] = {}
    for segment in sorted(segments, key=lambda item: (item.start, item.end, item.source_role)):
        mapping.setdefault(segment.speaker, f"speaker-{len(mapping) + 1}")
        segment.speaker = mapping[segment.speaker]
    canonical = {}
    identities = {}
    for speaker, profile in profiles.items():
        handle = mapping.get(speaker)
        if handle is None:
            continue
        profile.speaker = handle
        canonical[handle] = profile
        if profile.identity:
            identities[handle] = profile.identity
    return canonical, identities


def write_text(value: str, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(value)
    temporary.replace(path)
