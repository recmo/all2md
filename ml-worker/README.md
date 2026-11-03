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
| Transcription | Existing speech2md pipeline | Transcript Markdown and voiceprints |
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

## Development sequence

- [ ] Extract the existing worker lifecycle into an independent runner while
  preserving the transcription behavior and CLI until callers are updated.
- [ ] Dispatch by recipe, advertise installed capabilities, and reject jobs the
  machine cannot execute. Keep model dependencies with their owning projects.
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
