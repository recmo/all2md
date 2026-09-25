# Speech review integration proposal

Status: draft design; the integration described here is not implemented.

Bring speech-review into webui while keeping mdstore a client-agnostic document
server. Recordings belong to the managed repository, transcripts are derived
documents, and human review guidance remains versioned source data. A worker can
run on an intermittently available MacBook without blocking browsing or edits.

## Existing contracts

- `speech-review` writes adjacent `.hint.yaml` files, including speaker ranges,
  hotwords, metadata, and localized corrections. It does not edit transcripts.
- `speech2md` already supports Meeting Capture manifests and multiple tracks,
  verifies source checksums, and uses MLX for MOSS on Apple Silicon.
- Transcript timing comments and centisecond offsets are a playback contract.
  Preserve them when porting the review interface.
- MOSS caches can avoid repeated inference when their compatibility checks pass.
- The current speech-review queue is process-local. It cannot provide durable
  scheduling across server restarts or worker disconnections.
- mdstore commits validated UTF-8 text using `hash-object --no-filters`. Adding
  LFS attributes alone will not make this write path handle recordings.

## Storage

Use ordinary Git for capture manifests, review hints, schemas, and generated
Markdown. Use Git LFS for canonical audio/video payloads: repository paths carry
standard content-addressed pointers, while large bytes live in LFS storage.
Keep original recordings immutable; replacing one produces a new object.

Git can store binary files directly, but a growing recording archive makes every
ordinary clone expensive. GitHub also blocks regular Git files above 100 MiB.
LFS supports a separately configured server, so this does not require storing
private recordings on GitHub. Select and configure the actual LFS endpoint
separately; do not infer it from this source repository's remote.

Add explicit binary ingestion rather than enabling arbitrary Git clean filters
on the validated text path. Stream uploads into temporary storage, enforce size
and access limits, verify their digest, and durably store the payload before
committing its pointer. Failed commits can leave unreferenced objects for later
garbage collection. Backups must include LFS objects as well as Git history;
objects referenced by retained history must not be collected.

Expose asset metadata through the existing document inventory/MCP semantics.
Use authenticated streaming HTTP transfers with Range support for playback and
worker downloads, rather than base64 through text tools. Resolve authorization
against repository paths, including historical reads; an object hash is not an
access capability. Apply the same checks to LFS-backed and ordinary assets.

Webui should cache document metadata and text normally. Audio downloads are
on demand, with an explicit offline-download option; corpus synchronization must
not download the entire recording archive.

Keep model downloads, scratch files, MOSS caches, and voiceprint artifacts out of
Git history. Store reusable derived artifacts by input fingerprint, retaining
the currently published voiceprints for review. They can be rebuilt from source;
human hints cannot. Use existing cache compatibility checks inside speech2md.

## Desired outputs and durable jobs

Start with a fixed, versioned speech2md recipe, not an arbitrary workflow engine
or shell commands supplied by documents. Keep scheduling infrastructure generic;
speech-specific formats and execution remain in speech2md and its adapter.

A desired-output fingerprint includes every actual track's content hash, the
capture manifest, hints (including absence), processing configuration, and the
selected pipeline/model revisions. Hashing only a manifest or file timestamps
is insufficient. The desired recipe version is server configuration: installing
a newer worker must not silently retranscribe the whole archive.

On committed input changes, discover affected outputs and upsert durable jobs.
Unsubmitted browser drafts do not launch background work. Coalesce rapid changes
and deduplicate equivalent fingerprints. On startup, reconcile desired outputs
against published provenance so missing work can be rediscovered.

Keep queue state, attempts, leases, progress, and errors in a private operational
database outside Git. Retain the previous transcript while its replacement is
pending. Distinguish unprocessed, current, stale, queued, running, and failed
from the separate question of whether human review is complete.

Workers pull jobs over authenticated connections and advertise supported recipes,
backends, model revisions, and concurrency. The initial worker wraps existing
speech2md on Apple Silicon; other GPU backends need their own verified support.
The worker decides when it is available, allowing local idle/power policy.

Claiming work grants an expiring lease and a unique attempt token. Heartbeats
renew it; disconnection or sleep eventually permits another attempt. Retry
transient failures with backoff and retain actionable terminal errors. Duplicate
execution is possible, but publishing a result must be idempotent and reject
expired or superseded attempt tokens.

Workers receive immutable input references, download and verify them, and run in
isolated scratch directories. Worker credentials grant access only to assigned
inputs and result submission, not general repository writes. Reuse compatible
local caches and translate existing speech2md progress into job progress.

Before publication, mdstore verifies the active lease, input fingerprint, output
integrity, and expected output revision, then runs ordinary schema validation
and commits the result atomically. A change to hints, audio, schema, or the output
while inference runs must be reconciled before publication. A late result must
never overwrite a newer transcript or human edit. Useful stale cache artifacts
may remain reusable without publishing stale Markdown.

## Review interface

Render a dedicated Svelte review component for transcript/recording metadata.
Reuse webui navigation, staged changes, validation, submission, and permissions.
Port track playback, seeking, speaker lanes, assignments, range splitting,
voiceprint suggestions, and localized corrections from speech-review.

Review edits stage `.hint.yaml` changes through the existing changeset. Preserve
the current timing and hint formats. Generated transcripts remain read-only in
ordinary editing unless the user explicitly detaches a copy from regeneration;
never silently overwrite hand-edited prose. Unexpected external output changes
produce a conflict instead of being treated as an invitation to overwrite.

Show processing status and the last successful transcript together. A worker
being offline must not make existing transcripts unavailable. Previewing hints
locally is independent of publishing and scheduling their regeneration.

## Implementation sequence and acceptance checks

1. Add binary assets and LFS storage/streaming. Verify digest failures, interrupted
   uploads, authorization, byte ranges, and text-only offline synchronization.
2. Add desired-output records and durable leases. Test restart recovery,
   coalescing, duplicate completion, expired leases, and stale input/output races.
3. Add a speech2md worker adapter. Exercise a small fixture end to end through
   upload, claim, transcription, validated publication, and hint regeneration.
   Test cache reuse and a MacBook worker disconnecting before completion.
4. Port the review interface with playback and staged hint edits. Test timing,
   multi-track selection, correction persistence, and mobile playback.
5. Remove the standalone speech-review server and its process-local queue after
   the replacement covers the existing review behavior. Keep speech2md usable
   independently of webui.

No recordings are migrated, LFS host configured, or inference launched by this
proposal. The first implementation should complete one recording's lifecycle
before adding broader scheduling policy or additional GPU backends.

## References

- [GitHub regular Git file limits](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)
- [Git LFS pointer format](https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md)
- [Git LFS server configuration](https://github.com/git-lfs/git-lfs/blob/main/docs/api/server-discovery.md)
- [Existing review behavior](../speech-review/README.md)
- [Existing transcription pipeline](../speech2md/README.md)
