# MacBook ML worker

Development plan for a worker that runs on an Apple Silicon MacBook and picks
up embeddings, OCR, and transcription jobs from mdstore.

This work continues [PR #36](https://github.com/recmo/all2md/pull/36). The existing
[speech2md worker](../speech2md/src/speech2md/worker.py) is the starting point;
the shared runner and the OCR and text embedding recipes are planned work.

## Existing foundation

- mdstore owns durable jobs, expiring leases, frozen inputs/references, and
  validated atomic publication. Processing declarations live in `config.yaml`.
- Workers claim supported recipes through `POST /worker`, send heartbeats,
  download assigned inputs, and upload declared outputs using leased file URLs.
- `speech2md-worker` runs transcription in a subprocess, verifies input hashes,
  retains local caches, reports progress, and stops inference after lease loss.
- The worker uses a separate credential and never writes the server repository
  directly. The server remains responsible for validation, commits, and indexing.

See [the worker instructions](../speech2md/README.md#mdstore-worker) and
[the server documentation](../mdstore/README.md) for the current implementation.

## Intended behavior

One runner advertises only recipes available on this MacBook, claims one job at
a time, and dispatches to a local recipe implementation. Start with serial
execution to keep model memory use bounded. Each recipe owns its model loading,
input interpretation, checkpoints, and output generation; shared code handles
transport, leases, transfers, progress, cancellation, and publication.

| Job | Local processing | Result |
| --- | --- | --- |
| Transcription | Existing speech2md pipeline, with evaluated audio preprocessing | Transcript Markdown and voiceprints |
| OCR | Existing pages2md pipeline | Markdown and declared supporting assets |
| Embeddings | Explicitly selected local model | Vectors with model/version, dimensions, and source identity |

Recipe IDs must describe reproducible processing, including model and relevant
configuration. The runner must reject unknown recipes before starting inference.
Embedding collections must keep incompatible spaces separate. Text embeddings
need an explicit ingestion contract; existing voiceprint search alone does not
provide text embedding jobs or update the document search index.

MacBook sleep, shutdown, and network loss are normal interruptions. A lost lease
must prevent publication, stop local inference, and allow a fresh attempt to
resume compatible checkpoints. Input or recipe changes must invalidate stale
results. Run in the foreground first, then add a launchd service with restart
behavior and explicit paths for credentials, logs, and caches.

## Audio preprocessing

Preprocessing belongs inside the speech2md recipe so it shares the assignment,
lease, input verification, and checkpoint lifecycle. Transcription, diarization,
and speaker embeddings already exist; the following work extends that pipeline.
LLM-based summaries, captions, and extraction remain with external providers.

The current pipeline selects each declared audio stream and converts model inputs
to 16 kHz mono. It also detects silence and falls back to low-energy boundaries
when splitting long recordings. These steps are already implemented. Learned
voice activity detection (VAD) and denoising are not currently implemented.

Evaluate additions in this order:

1. **Audio inspection:** record duration, channel layout, sample rate, clipping,
   and level statistics per track. Report silent or problematic inputs without
   changing the canonical recording.
2. **VAD:** evaluate speech-region detection for chunk boundaries and selecting
   speaker embedding samples. Retain context around speech and keep the existing
   silence-based behavior as the baseline. Do not discard quiet speech or overlap
   based solely on a VAD decision.
3. **Level adjustment:** evaluate bounded gain and peak protection for quiet
   tracks. Record the processing parameters and compare with unmodified input.
4. **Noise suppression:** evaluate an optional pinned speech-enhancement model
   for noisy recordings. Compare raw and enhanced inputs for both transcription
   and speaker matching before making enhancement a default for any recipe.

Preserve microphone and participant roles and process their streams separately.
Every derived waveform must retain the original time axis and duration, including
inter-track offsets. Do not concatenate detected speech or remove silence;
transcript timestamps, attendee ranges, corrections, and playback must continue
to refer to the canonical recording. If a future transform introduces delay,
compensate and test alignment explicitly.

Keep originals immutable and enhanced audio in the local recipe cache unless
explicitly declared as an output. Include source hashes, model/checkpoint identity,
parameters, and preprocessing version in cache keys and publication provenance.
Only reuse checkpoints produced by a compatible preprocessing configuration.
Choose the transcription and speaker-embedding input paths independently based
on evaluation; enhancement need not be used for both.

Before enabling a stage, compare against the current pipeline on clean, noisy,
quiet, overlapping, and silent audio, including separate-role captures. Check
transcript word errors, diarization errors, speaker-match accuracy, timestamp
alignment, and runtime/memory use on the MacBook. Fixture tests can verify timing,
cache invalidation, cancellation, and no-speech handling; actual quality decisions
require representative recordings and reviewed reference transcripts/identities.
Do not add source separation or dereverberation to the initial implementation;
revisit them only if recordings demonstrate a need.

## Development sequence

- [ ] Extract the existing worker lifecycle into an independent runner while
  preserving the transcription behavior and CLI until callers are updated.
- [ ] Dispatch by recipe, advertise installed capabilities, and reject jobs the
  machine cannot execute. Keep model dependencies with their owning projects.
- [ ] Evaluate and implement audio preprocessing in the order above, retaining
  the existing path until quality and alignment checks justify each stage.
- [ ] Add an OCR adapter that downloads the assigned source, invokes pages2md,
  and publishes only the declared Markdown and supporting assets.
- [ ] Define embedding input/output schemas, versioned model identity, and the
  server ingestion path; add a local embedding adapter against that contract.
- [ ] Package the runner for `aarch64-darwin` in Nix and document foreground and
  launchd operation, cache retention, and model downloads.
- [ ] Verify idle polling, successful execution/publication, stale input fencing,
  lease expiry, interrupted transfers, sleep/restart recovery, and replay after
  a lost completion response using fixture recipes without downloading models.
- [ ] Run actual transcription, OCR, and embedding smoke jobs on an Apple
  Silicon MacBook and record model versions and resource use before deployment.

The current PR has no live MacBook inference or deployment validation. New
adapters and service setup must be verified as they are implemented.
