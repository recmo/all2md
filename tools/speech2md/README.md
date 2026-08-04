# speech2md

`speech2md` turns local meeting recordings into speaker-attributed Markdown.
It uses MOSS for transcription and window-local diarization:
[`OpenMOSS-Team/MOSS-Transcribe-Diarize`](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize),
pinned to revision `e8681d68e7042738ffca8ac8212bc8fcb1131ab8`. It uses
[ReDimNet2 B6](https://github.com/PalabraAI/redimnet2) to reconcile those local
speaker labels across windows, pinned to source revision
`cdc875670034dd7068013ca2ab21ec083a040ff8` and the `vb2+vox2_v0-lm`
checkpoint with SHA-256
`e0a7d340a92f798720d1208949aa6a6bd0cddcb0ba7d4cec33596a17a484e6a2`.

The CLI exposes only this MOSS + ReDimNet2 pipeline.

The Nix package includes its locked Python dependency environment in the
immutable Nix store. It does not create a virtual environment or install
packages at runtime; only model weights and checkpoints populate user caches.

```sh
uv sync --project tools/speech2md --extra dev
uv run --project tools/speech2md pytest tools/speech2md/tests
uv run --project tools/speech2md speech2md transcribe meeting.mp4
uv run --project tools/speech2md speech2md transcribe meeting.mp4 \
  --hotwords 'Alice, Bob, ProveKit, F2Z, ReDimNet2'
uv run --project tools/speech2md speech2md relabel meeting.mp4 'Speaker 1=Alice'
uv run --project tools/speech2md speech2md render meeting.mp4
uv run --project tools/speech2md speech2md benchmark ~/Documents/Meetings
```

The input may be an ordinary audio/video file or a Meeting Capture v1 manifest.
Manifest checksums are verified before inference. Canonical audio is never
changed.

MOSS always receives the canonical English timestamp-and-speaker prompt.
Optional hotwords are appended using the documented `Hotwords:` form. Supply a
targeted, comma-separated list of names, acronyms and unusual domain terms with
`--hotwords`; duplicates are removed case-insensitively and the list is capped
at 40 entries. The exact prompt and normalized list are retained in both the
processing provenance and raw MOSS artifact.

Long recordings use one deliberately fixed policy. `speech2md` chooses the
minimum number of roughly equal parts targeting 30 minutes, moves each ideal
boundary to a detected silence within one minute, and adds two seconds of audio
overlap. The silence and window parameters are source constants, not CLI
options. If no nearby silence exists, processing stops; there is no alternate
windowing method. The progress bar advances whenever MOSS emits a timestamp,
then fills trailing silence when the window completes. It remains monotonic
across overlapping recovery passes. Generation token counts are retained in
the raw MOSS artifact for diagnosis; prompt and total counts are null because
the streaming MOSS API does not expose them. Generation is capped at 16,384
tokens because
local runs show the model emitting its end token around that practical horizon
even when a larger runtime limit is supplied. Independent upstream reports
describe the same premature ending on long audio in
[issue #26](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/issues/26) and
[issue #34](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/issues/34);
they corroborate the behavior but do not establish its exact cause.

`speech2md` therefore does not trust the end token by itself. Any pass producing
14,000 or more tokens is considered incomplete and triggers recovery; lower
token counts are accepted regardless of the gap between the final speech
timestamp and the submitted window's end, so trailing silence does not look
like missing transcription. Processing resumes 30 seconds before the last
complete timestamp and merges the overlap at its midpoint. Recovery repeats
only while timestamp coverage moves forward by at least five seconds, and is
capped at eight recovery passes per planned window. Invalid timestamps, stalled
recovery or exhaustion of that cap stop processing with an error instead of
producing an apparently complete but truncated transcript. Every generated
segment must fit within the exact submitted audio span. The raw artifact records
each pass, its requested range, last complete timestamp, remaining diagnostic
gap, token-risk status, recovery decision, parse status and token counts.

ReDimNet2 reconciles speakers between parts using cosine similarity. The match threshold is `0.65`,
with a required `0.08` margin over the second-best profile and one-to-one
assignment inside a window. Missing or ambiguous evidence creates a new
anonymous speaker; it never falls back to transcript text.

Audio overlap remains only for transcript boundary trimming and deduplication.
The raw per-window MOSS output and reconciliation decisions are retained in
`*.moss.json` for audit and future reprocessing.

The generated `*.speech2md.json` is the editable processing state. Markdown is a
disposable rendering of it, and `relabel` changes speaker names without
retranscribing. Mechanical capture and processing details are retained in YAML
frontmatter so the visible document body contains only the title and transcript.
The state retains multiple 192-dimensional vectors per speaker,
including their source track, window and absolute timestamps. They are
meeting-local evidence only: cross-meeting enrollment, identity matching and
automatic names are future work. Vectors are not written to Markdown.
States created under the earlier `*.audio2md.json` or transitional
`*.voice2md.json` names remain readable and editable; new transcriptions use
`*.speech2md.json`.

The ReDimNet2 model and checkpoint load lazily on the first usable participant
sample and are reused for the run. The first run requires network access to
populate the PyTorch model cache (`$XDG_CACHE_HOME/speech2md/torch` through the
Nix wrapper). A load, checksum or inference failure stops processing clearly;
canonical audio remains unchanged. Existing derived artifacts require
`--force` to replace.
