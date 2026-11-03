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
| Transcription | Existing speech2md pipeline, with optional enhanced input | Transcript Markdown and voiceprints |
| Audio enhancement | Level adjustment and optional denoising per track | Playable enhanced audio and processing provenance |
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

## Audio enhancement for playback and model input

Add level adjustment and optional denoising as a reusable worker stage with two
consumers: WebUI playback and speech2md model input. Transcription, diarization,
and speaker embeddings already exist. LLM-based summaries, captions, and
extraction remain with external providers.

The current pipeline selects each declared audio stream and converts model inputs
to 16 kHz mono. It detects silence and falls back to low-energy boundaries when
splitting long recordings. Learned VAD, level adjustment, and denoising are new
work. MOSS and ReDimNet2 do not prescribe external enhancement in their documented
inference paths; model-input enhancement is an explicit recipe choice that needs
comparison against the original input, rather than a model requirement.

The enhancement stage should:

- Inspect duration, channel layout, sample rate, clipping, and levels per track.
- Apply bounded level adjustment with peak protection; detect silent input so
  normalization does not amplify it into noise. Record the actual applied gain.
- Support optional denoising with a pinned model/checkpoint and recorded settings.
- Preserve microphone and participant roles and process streams separately.
- Retain the original time axis and duration, including inter-track offsets.
  Compensate model delay and test alignment. Do not remove silence, concatenate
  speech, or use VAD to gate the initial enhancement implementation.
- Keep canonical recordings immutable and publish enhanced playback files only
  as declared derived outputs through the existing lease/publication lifecycle.

### WebUI playback

Publish browser-playable enhanced audio for each declared role, with explicit
links to its source and processing version. For shared multi-track containers,
produce separate playable track files so the browser selects the correct stream.
Playback derivatives should retain suitable listening fidelity; the models' mono
16 kHz conversion is a separate consumer-specific step.

Offer an Original/Enhanced selection when a current enhanced artifact exists.
Label the selected version and preserve playhead position, seeking, playback
speed, and transcript highlighting when switching. Changes to playback selection
must not trigger transcription. Keep original playback available; if enhancement
is missing, stale, or failed, show its status and use playable original audio
where available. Never substitute a different role when a source stream cannot
be played by the browser.

### Model input

Choose enhancement for transcription and speaker embeddings independently in the
recipe. Reuse the same enhancement implementation and compatible cached audio
used to generate playback assets. Convert to each model's required format after
enhancement. Enhanced playback must work independently of model-input enhancement;
neither setting implicitly enables the other.

Key caches and publication provenance by source hashes, track/stream identity,
preprocessing version, model/checkpoint identity, and parameters. A recipe or
input change must invalidate affected enhancement and inference checkpoints and
fence stale publication. Changing a playback preference does not change these
keys. Share lease renewal, cancellation, transfer verification, and atomic output
publication with the worker lifecycle.

### Validation

Compare original and processed audio on clean, noisy, quiet, overlapping, clipped,
and silent fixtures, including separate-role captures. Check output levels, peak
protection, duration, offsets, and browser playback/seeking. Review listening
quality separately from transcript word errors, diarization errors, and
speaker-match accuracy before selecting model-input defaults. Measure runtime
and memory on the MacBook. Quality decisions require representative recordings
and reviewed reference transcripts/identities; fixture tests cover alignment,
cache invalidation, cancellation, publication, and no-speech handling.

Leave learned VAD, source separation, and dereverberation for later evidence of
need. VAD is not a prerequisite for level adjustment or denoising.

## Development sequence

- [ ] Extract the existing worker lifecycle into an independent runner while
  preserving the transcription behavior and CLI until callers are updated.
- [ ] Dispatch by recipe, advertise installed capabilities, and reject jobs the
  machine cannot execute. Keep model dependencies with their owning projects.
- [ ] Add per-track level adjustment and optional denoising, publish enhanced
  playback artifacts, and add independent model-input recipe settings.
- [ ] Add Original/Enhanced playback selection to WebUI with aligned seeking,
  original fallback, and enhancement status; verify both playback and model quality.
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
