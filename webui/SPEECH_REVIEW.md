# Recording review and derived documents

Recording review now lives in webui. `speech2md` consumes authored recording
Markdown directly and `speech2md-worker` pulls durable jobs from mdstore. The
standalone speech-review server and hint-sidecar input format are removed.

## Recording lifecycle

1. Add a document with `mdstore: recording` frontmatter and either `audio:
   audio.m4a` or `manifest: capture.json`. Paths are relative to the document.
   The Markdown body contains human notes. Start from
   [the recording example](../mdstore/examples/meetings/example/recording.md)
   and [its schema](../mdstore/examples/meetings/schema.md).
2. Submit the document, upload its source audio from the recording
   view, and choose **Enable transcription**. Capture manifests and their
   constituent files can also be uploaded through `PUT /<path>` with `If-None-Match: *`.
3. Run a worker with a separate `MDSTORE_WORKER_TOKEN`:
   `speech2md-worker --server https://your-store.example`. The server must have
   the same worker token configured. This starts no worker on browser clients.
4. The worker downloads assigned inputs, verifies their hashes, runs speech2md,
   uploads voiceprints, and submits the transcript. mdstore validates the result
   with the current schemas and links before publishing it atomically.
5. Review timed turns, seek in audio, assign speaker ranges and person paths,
   split a range at the playhead, and enter hotwords or localized corrections.
   These operations stage ordinary edits to recording frontmatter. Submit them
   using the existing changeset. Committed guidance changes enqueue new work;
   changing human notes or YAML formatting does not.

Published transcripts remain readable while regeneration is pending. They are
ordinary validated and indexed Markdown documents, but all ordinary edit,
delete, rename, and backlink-rewrite operations reject their owned paths.
Corrections belong in the recording document. `backlinks(required=False)` in a
target schema waives authored reciprocal links without disabling target checks
or removing graph edges.

**Regenerate** requests another run, **Retry** restarts failed work, and **Update
source inputs** refreshes the explicit asset assignment after committing a new
source reference. Replacing source bytes uses `PUT /<path>` with the current `If-Match` ETag;
changed input hashes invalidate in-flight results. Deleting a
derivation and its outputs is an explicit API operation; ordinary document
editing cannot remove ownership.

## Storage and provenance

`.mdstore-artifacts.json` is committed and owns asset paths, derivation definitions,
selected frontmatter fields, recipes, output paths, and publication provenance.
Source audio/video and JSON voiceprints use standard Git LFS pointers and objects.
Published Markdown stays in ordinary Git. Each publication records the exact
source revision, input fingerprint, output hashes, worker attempt, and frozen
reference objects. External changes to owned output bytes fail provenance checks.

Operational jobs, leases, progress, and retained candidate results live under
`.git/mdstore/`. LFS objects live under `.git/lfs/objects/`. Back up both Git history
and LFS objects; pushing Git commits alone does not transfer these objects.
Compatible MOSS inference caches stay on the worker, outside Git.

Jobs reconcile from committed definitions and input fingerprints when workers
poll or clients read status. Leases expire after five minutes and renew every
30 seconds. Failed attempts retry with backoff up to three failures; explicit
retry resets that limit. Superseded leases cannot publish. The previous valid
publication survives candidate validation failure. Removing operational job state
reconstructs work from committed provenance.

## Cross-meeting speaker references

Voiceprints are JSON artifacts containing normalized embeddings, speaker handles,
confirmed person paths, and an embedding space identified by namespace, model
recipe, and dimensions. They reuse mdstore's generic exact cosine collection;
text embeddings and speaker embeddings cannot mix.

A job freezes eligible current reference artifacts at claim time and excludes
its own outputs. Worker vector queries are restricted to that frozen set and
filter to confirmed identities before ranking. Matches become review suggestions,
never automatic confirmations. Publication checks that frozen evidence is still
current. Adding a meeting does not retranscribe the archive.

## Current limits

- The Apple Silicon speech2md backend is supported by the worker; no GPU model
  inference is run by the integration tests. Keep workers on a pinned pipeline
  and model revision for a given recipe name.
- Per-file capture tracks support browser playback. Shared multi-track containers
  are processed by the worker, but browser track-specific playback is disabled
  rather than playing the wrong stream. There is no waveform editor yet.
- Corrected references fence in-flight results and are excluded from new jobs.
  Already published identification suggestions require explicit regeneration;
  automatic matching-only invalidation is not implemented.
- Rejected candidates are retained for diagnosis. Retrying currently reruns the
  pipeline with its compatible inference cache; there is no candidate-only
  revalidation endpoint.
- Audio is streamed on demand and is not included in automatic offline caching.
- The server implements Git LFS basic upload/download. No external LFS hosting or
  client Git remote is configured automatically.
