from __future__ import annotations

from pathlib import Path
import re
import tempfile
import time

import yaml
from tqdm.auto import tqdm

from . import __version__
from .media import probe, resolve_input, sha256
from .model import TranscriptState
from .moss import (
    build_transcription_prompt,
    load_moss_engine,
    normalize_hotwords,
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
    hotwords: list[str] | None = None,
) -> TranscriptState:
    hotwords = normalize_hotwords(hotwords)
    if not re.fullmatch(r"[0-9a-f]{40,64}", __version__):
        raise RuntimeError("speech2md source commit is unavailable")
    prompt = build_transcription_prompt(hotwords)
    resolved = resolve_input(requested)
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
                progress_callback=report_progress,
            )
            segments.extend(track_segments)
    speaker_profiles = _canonicalize_speakers(segments, speaker_profiles)
    state = TranscriptState(
        title=resolved.title,
        started_at=resolved.started_at,
        ended_at=getattr(resolved, "ended_at", None),
        calendar_event=getattr(resolved, "calendar_event", None),
        source_sha256=source_hash,
        processing_seconds=time.monotonic() - started,
        segments=sorted(segments, key=lambda item: (item.start, item.end, item.source_role)),
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
    for speaker, profile in profiles.items():
        handle = mapping.get(speaker)
        if handle is None:
            continue
        profile.speaker = handle
        canonical[handle] = profile
    return canonical


def write_text(value: str, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(value)
    temporary.replace(path)


def relabel(requested: Path, mappings: list[str]) -> dict[str, str]:
    markdown_path = requested.expanduser().resolve()
    if markdown_path.suffix.lower() != ".md":
        raise ValueError("relabel input must be a Markdown file")
    if not markdown_path.is_file():
        raise FileNotFoundError(markdown_path)
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
