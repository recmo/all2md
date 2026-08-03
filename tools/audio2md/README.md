# audio2md

`audio2md` turns local meeting recordings into speaker-attributed Markdown.
It uses one model:
[`OpenMOSS-Team/MOSS-Transcribe-Diarize`](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize),
pinned to revision `e8681d68e7042738ffca8ac8212bc8fcb1131ab8`.

MOSS jointly transcribes and diarizes. The CLI exposes only this pipeline.

```sh
uv sync --project tools/audio2md --extra dev
uv run --project tools/audio2md pytest tools/audio2md/tests
uv run --project tools/audio2md audio2md transcribe meeting.mp4
uv run --project tools/audio2md audio2md relabel meeting.mp4 'Speaker 1=Alice'
uv run --project tools/audio2md audio2md render meeting.mp4
uv run --project tools/audio2md audio2md benchmark ~/Documents/Meetings
```

The input may be an ordinary audio/video file or a Meeting Capture v1 manifest.
Manifest checksums are verified before inference. Canonical audio is never
changed.

Long recordings are processed in five-minute windows with five seconds of
overlap. Speaker labels are joined across adjacent windows only when MOSS emits
matching speech in the overlap. Otherwise a new anonymous speaker label is
created; uncertain speakers are never guessed. The raw per-window MOSS output
is retained in `*.moss.json` for audit and future reprocessing.

The generated `*.audio2md.json` is the editable processing state. Markdown is a
disposable rendering of it, and `relabel` changes speaker names without
retranscribing. Existing derived artifacts require `--force` to replace.
