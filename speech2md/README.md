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
uv sync --project speech2md --extra dev
uv run --project speech2md pytest speech2md/tests
uv run --project speech2md speech2md meeting.mp4
```

The input may be an ordinary audio/video file or a Meeting Capture manifest.
Both legacy v1 per-file tracks and v2 Matroska containers are accepted. For v2,
each role is read from its declared audio stream in the shared container.
Manifest checksums are verified before inference. Canonical audio is never
changed.

MOSS always receives the canonical English timestamp-and-speaker prompt.
Optional hotwords and manually identified speaker ranges come from an adjacent
hint file. For `meeting.mp4`, `speech2md` reads `meeting.hint.yaml` when it
exists:

```yaml
hotwords:
  - ProveKit
  - F2Z
  - ReDimNet2
attendees:
  - handle: Alice
    identity: ''
    ranges:
      - track: mixed
        start: 754.0
        end: 762.0
```

Both fields are optional, as are `ranges` on each attendee. Unknown fields, invalid tracks,
out-of-bounds ranges, and identities that cannot be separated at a MOSS turn
boundary stop processing. A track may be omitted for single-track media and is
required for multi-track captures. Hotwords are trimmed, deduplicated
case-insensitively, and capped at 40. The hint file is never rewritten.
Raw generations are retained in an adjacent cache as described below.

Long recordings use one deliberately fixed policy. `speech2md` chooses the
minimum number of roughly equal parts targeting 30 minutes, moves each ideal
boundary to a detected silence within one minute, and adds two seconds of audio
overlap. If no qualifying silence exists, it uses the lowest-energy 100 ms
point in the same search range and increases the overlap to six seconds. The
silence, energy, overlap, and window parameters are source constants, not CLI
options. The progress bar labels model prefill explicitly, switches to
transcribing with the first streamed token, advances whenever MOSS emits a
timestamp, then fills trailing silence when the window completes. It remains
monotonic across overlapping recovery passes. Generation token counts are
available to the in-process recovery logic; prompt and total counts are null
because the streaming MOSS API does not expose them. Generation is capped at
16,384 tokens because
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
segment must fit within the exact submitted audio span. Each pass retains its
requested range, last complete timestamp, remaining diagnostic gap, token-risk
status, recovery decision, parse status and token counts only until the run
finishes.

ReDimNet2 reconciles speakers between parts using cosine similarity. The match threshold is `0.65`,
with a required `0.08` margin over the second-best profile and one-to-one
assignment inside a window. Missing or ambiguous evidence creates a new
anonymous speaker; it never falls back to transcript text.

Audio overlap remains only for transcript boundary trimming and deduplication.
Successful transcription publishes three derived files:

```text
meeting.md
meeting.moss.npz
meeting.voiceprints.npz
```

The MOSS cache stores replayable raw generations and is accepted only when its
schema, audio-track checksums, roles and stream indexes, normalized ordered hotwords, pinned
MOSS model revision, prompt, generation limit, and window/recovery settings all match.
Changing metadata, attendees, speaker ranges, or localized edits therefore
normally reuses MOSS output while rerunning speaker reconciliation and rendering.
If explicit ranges show that one window-local MOSS label was collapsed across
two identities, `speech2md` re-decodes only that affected window with forced
speaker labels at the conflicting turn boundaries. The cache retains the
original unguided generation as `base` beside the guided variant and the exact
forces used. Further relabeling is cache-only unless it changes those boundary
forces; identity renames alone remain cache-only. `--require-moss-cache` reports
a cache miss when new guidance is required, allowing callers to move the work
to their normal model queue instead of blocking a fast-update path.
Changing hotwords or audio invalidates the cache. Invalid or unreadable caches
are ignored. The cache contains string arrays only and is loaded with
`allow_pickle=False`.

Markdown is the readable derived output. Its flat YAML front matter contains
the source hash, speech2md source commit, optional hint-file hash, authoritative
meeting start/end times when supplied by Meeting Capture, an optional
calendar-event link, and the attendee roster. `handle` is the editable
human-readable name; `identity` is reserved for a future unique person-document
path and is currently empty. The roster has no positional association with
transcript speakers. Every transcript run starts with a handle declared in this
roster. Identified turns use the human handle directly; unidentified turns use
a declared processing-local `speaker-N` handle. Those anonymous entries
disappear when regeneration replaces all of their runs with an identified
handle, while attendees without turns remain in the roster. ReDimNet2
propagates anchored handles conservatively within the recording.

```yaml
---
source_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
speech2md_version: 0123456789abcdef0123456789abcdef01234567
hints_sha256: abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789
started_at: 2026-08-04T10:00:00+02:00
ended_at: 2026-08-04T11:00:00+02:00
calendar_event: https://calendar.google.com/example
attendees:
  - handle: Alice
    identity: ""
  - handle: Michał
    identity: ""
  - handle: speaker-1
    identity: ""
---
```

Unavailable timestamps and calendar links are omitted rather than written as
null values.

Transcript run headers and invisible timing comments use the same centisecond
precision. Comment values are offsets from the run header; comments at retained
MOSS boundaries provide intermediate timing, and the final comment is the
authoritative run end.

```markdown
**[00:00:29.99] speaker-1:** How is everyone doing? <!-- 1.53s --> I think most of us is here. <!-- 3.69s -->
```

A run whose label is absent from `attendees[].handle` is invalid.

The NPZ contains only `handles`, a Unicode array of shape `(S,)`, and
`embeddings`, a float32 array of shape `(S, 192)`. Each row is a robust,
unit-normalized aggregate of the clean ReDimNet2 samples for the corresponding
handle. It is loaded with `allow_pickle=False` and written with owner-only file
permissions. A recording without usable voice evidence gets stable empty shapes
`(0,)` and `(0, 192)`.

Recovery records, reconciliation decisions, and individual embedding samples
remain unpublished intermediates. A run that completes MOSS but fails during a
later hint or rendering stage may still publish the reusable MOSS cache.
Markdown is never edited in place to change speaker identities; update the hint
file and derive it again with `--force`.

The ReDimNet2 model and checkpoint load lazily on the first usable participant
sample and are reused for the run. The first run requires network access to
populate the PyTorch model cache (`$XDG_CACHE_HOME/speech2md/torch` through the
Nix wrapper). A load, checksum or inference failure stops processing clearly;
canonical audio remains unchanged. Existing derived artifacts require `--force`
to replace.
