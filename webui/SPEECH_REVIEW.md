# Speech review integration proposal

Status: integration in progress. The first implementation provides a reusable
exact vector collection in `mdstore::vectors`, sharing cosine scoring with text
search. Asset ingestion, voiceprint loading, worker transport, and the review UI
remain unimplemented; the following sections describe their intended design.

Accepted decisions: use Git LFS for audio/video, commit published derived
documents to Git, and enforce their read-only status in mdstore itself. Store
authored review guidance in `recording.md` frontmatter, not a separate hint
sidecar. Modify speech2md to consume that format directly, without legacy hint
file support or a translation layer. Let the target schema exempt its documents
from authored backlinks.

Bring speech-review into webui while keeping mdstore a client-agnostic document
server. Recordings belong to the managed repository, transcripts are derived
documents, and human review guidance remains versioned source data. A worker can
run on an intermittently available MacBook without blocking browsing or edits.

## Existing contracts

- `speech-review` writes adjacent `.hint.yaml` files, including speaker ranges,
  hotwords, metadata, and localized corrections. It does not edit transcripts.
  Replace this sidecar format with recording-document frontmatter as part of the
  integration; the existing format is not a backward-compatibility requirement.
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

Keep model downloads, scratch files, and MOSS inference caches out of Git
history. Use existing cache compatibility checks inside speech2md. Distinguish
these disposable intermediates from published derived documents: expensive
outputs are not disposable merely because they can theoretically be rebuilt.
Persist published voiceprint artifacts needed by review as versioned LFS assets,
associated with the same publication as their transcript.

### Cross-meeting speaker references

Use separate embedding spaces for document and speaker vectors, identified by
namespace, full embedding recipe version, and dimensions. Reuse mdstore's exact
cosine search without a new database. Direct vector queries bypass text embedding,
graph expansion, and reranking, preserving reference provenance in each result.
Apply the trusted job's reference filter before selecting the best matches. The
index is rebuildable from published artifacts and is not another source of truth.

The initial Rust library collection validates dimensions, finite/nonzero vectors,
and unique record IDs, and rejects queries from incompatible embedding spaces.
Metadata is caller-owned so mdstore's search primitive stays independent of
speech schemas. The future worker endpoint must authenticate requests and supply
the reference filter; the library itself is not an authorization service.

Workers may use published voiceprints from other authorized meetings to assist
participant identification. Assign each job an immutable reference manifest
containing artifact content hashes, embedding model/preprocessing versions, and
the revision of the human-confirmed speaker-to-person assignments. Use stable
person-document identities, not display names, to associate speakers across
meetings. Only compare compatible embeddings. An unconfirmed automatic match
must not become trusted reference evidence for later meetings; return uncertain
matches as suggestions and allow an unknown speaker.

Reference artifacts are additional declared job inputs. Extend the job's scoped
read capability to its authorized reference set, without granting unrestricted
repository access or requiring downloads of the other recordings. Workers can
cache these immutable artifacts by content hash. Keep reference selection within
the configured authorization boundary; cached bytes do not confer permission to
use a reference in a different job.

Record the exact reference manifest and matching recipe in provenance and in
the identification-stage fingerprint. Freeze the reference set when dispatching
an attempt so concurrent publications do not change its inputs mid-run. Exclude
the current recording's derived outputs from its own reference set. Keep matching
separate from expensive audio inference where possible: updated identities or
reference voiceprints should allow rematching cached embeddings, not force
retranscription. Validate confirmed assignments again before publishing an
identification result if its reference evidence has been corrected or revoked.

Adding a new meeting must not automatically regenerate every older meeting and
create a feedback loop. Initially, use current references for new or explicitly
requested identification jobs. Corrections to evidence used by an existing
result should mark its identification stale and schedule only the affected
matching work. Exact selection and matching policy belong to the speech recipe,
not to mdstore's generic job scheduler.

## Derived documents are persistent, read-only store members

Commit each successfully published derived Markdown document to ordinary Git.
Other published derived documents follow the same rule, using LFS for large
binary payloads. This preserves costly results, their exact historical bytes,
diffs, and rollback even when the original model or worker is unavailable.
Re-running a pinned model is not assumed to reproduce identical bytes.

Derived documents participate in normal inventory, reads, search/indexing,
schema and link validation, history, and offline text caching. Read-only is a
write policy, not an exclusion from the store. Existing access controls still
apply. Input changes mark a published result stale without removing it; staleness
and validation validity are separate properties.

Enforce ownership in mdstore using committed derivation metadata, including the
output paths, recipe and version, input fingerprint, and published content hashes.
This metadata must survive cloning and rebuilding operational state. Its exact
representation is an implementation detail; protection cannot depend only on a
frontmatter flag inside an editable output or on the operational job database.
Ordinary edits cannot remove or rewrite the ownership record to bypass protection.

Reject ordinary mutations of owned output paths, including replacement, deletion,
rename/move, and indirect writes such as automatic backlink rewriting. Check
ownership against the current committed state and the complete proposed batch;
a batch cannot first remove ownership and then edit an output. Expose the
effective read-only permission and derivation status through the existing API so
all clients can explain why editing is unavailable. Broad document write access
does not grant permission to overwrite derived outputs.

Only the authorized result-publication path can create or replace owned outputs.
Workers submit candidates, not arbitrary Git commits. Validate candidate outputs
and their affected dependents against the current repository using the normal
validation rules before atomically committing outputs and provenance. Failed
validation leaves the previous publication intact and exposes diagnostics.
Output ownership and lease checks are additional to validation, not exemptions
from it. Commit publications, never progress events or failed attempts.

Corrections belong in authored inputs such as review hints. A separately named
authored copy may be edited normally but is no longer the derivation's output.
Removing a derivation and its outputs requires an explicit lifecycle operation;
ordinary deletion must not implicitly disable protection. Direct filesystem or
Git writes bypass mdstore's API boundary and must be detected as provenance
mismatches, not silently overwritten by the next job.

## Desired outputs and durable jobs

Start with a fixed, versioned speech2md recipe, not an arbitrary workflow engine
or shell commands supplied by documents. Keep scheduling infrastructure generic;
speech-specific formats and execution remain in speech2md and its adapter.

A desired-output fingerprint includes every actual track's content hash, the
capture manifest's processing inputs, the recording frontmatter fields consumed
by transcription (including absent values), processing configuration, and the
selected pipeline/model revisions. Use a deterministic, versioned projection of
these inputs: editing human notes or YAML formatting must not trigger inference.
Hashing only a manifest or file timestamps is insufficient. Record source
revisions separately for provenance. The desired recipe version is server configuration: installing
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

Use an authored Markdown document for each recording, alongside its source
assets and derived transcript. For example:

```text
meetings/2026-09-25/
  recording.md
  audio.m4a
  transcript.md
```

The recording document's YAML frontmatter holds source references, metadata,
hotwords, attendees, speaker ranges, and localized corrections. Its Markdown
body holds optional human-authored notes. For multi-track recordings, reference
the capture manifest rather than flattening its tracks into a single audio file.
Recording discovery and classification should use schema/metadata, not require
this illustrative directory name or a particular recording date layout.

```yaml
---
audio: audio.m4a
hotwords: [ProveKit, F2Z]
attendees:
  - handle: Alice
    identity: /people/alice.md
    ranges:
      - track: participants
        start: 1122
        end: 1148
edits:
  - track: participants
    start: 1122
    end: 1148
    before: F two Z
    after: F2Z
---
# Weekly check-in

Optional human-authored notes.
```

Validate the structured guidance through the recording schema. Reuse normal
document staging, concurrency checks, submission, and frontmatter relations for
person references. Source references must resolve to managed assets/manifests,
not arbitrary worker filesystem paths. The example preserves existing hint
range and correction semantics; precise field declarations belong in the schema.

Modify speech2md to read the recording Markdown document directly. Its
frontmatter is the sole authored source of processing guidance, with the body
reserved for human notes. Share the same loader between standalone speech2md and
worker execution; do not generate intermediate `.hint.yaml` files. Remove legacy
sidecar discovery, loading, CLI options, documentation, and tests rather than
maintaining two input formats or fallback behavior. Update fixtures and examples
to the recording-document format. Existing data conversion, if needed, is a
separate one-time operation, not a compatibility path in the pipeline.

Render a dedicated Svelte review component for transcript/recording metadata.
Reuse webui navigation, staged changes, validation, submission, and permissions.
Port track playback, seeking, speaker lanes, assignments, range splitting,
voiceprint suggestions, and localized corrections from speech-review.

Review edits stage `recording.md` frontmatter changes through the existing
changeset, preserving unrelated frontmatter and the human-authored body. Preserve
the current transcript timing and review-guidance semantics. Generated transcripts remain read-only in
ordinary editing, enforced by mdstore as well as reflected in the UI. An authored
copy uses a distinct path outside the derivation's ownership. Unexpected external output changes
produce a conflict instead of being treated as an invitation to overwrite.

Show processing status and the last successful transcript together. A worker
being offline must not make existing transcripts unavailable. Previewing hints
locally is independent of publishing and scheduling their regeneration.

## Target-side backlink policy

Reciprocity considers both endpoint schemas. The source declares a reciprocal
relationship; the target schema can waive its obligation to author the reverse
edge. The meetings schema can therefore exempt its documents without exceptions
in every linking schema. Proposed declaration: `backlinks(required=False)`.

This exemption does not disable target/anchor validation or remove graph edges.
Computed incoming links remain available independently of authored backlinks.
Read-only status does not itself confer an exemption. Without a target exemption,
the source's reciprocal requirement continues to apply.

Changing the target policy must revalidate affected incoming relationships,
including sources outside the target directory, identically in server and WASM
validation. Resolve the policy using the existing effective-schema rules:
nearest schemas replace parents, so a nested replacement schema must explicitly
declare an exemption if it needs one. Do not introduce implicit inheritance for
this one setting.

## Implementation sequence and acceptance checks

1. Add binary assets and LFS storage/streaming. Verify digest failures, interrupted
   uploads, authorization, byte ranges, and text-only offline synchronization.
2. Add desired-output records and durable leases. Test restart recovery,
   coalescing, duplicate completion, expired leases, and stale input/output races.
   Verify that owned outputs remain readable, indexed, and validated while all
   ordinary write paths reject changes, including ownership-removal batches and
   backlink rewrites. Verify protection survives a clone and queue rebuild, and
   invalid worker results cannot replace a valid publication.
3. Update speech2md to consume recording documents directly and add a worker
   adapter. Exercise a small fixture end to end through
   upload, claim, transcription, validated publication, and hint regeneration.
   Test cache reuse and a MacBook worker disconnecting before completion.
4. Port the review interface with playback and staged recording-frontmatter edits.
   Test timing, multi-track selection, correction persistence, direct recording
   document loading in standalone and worker execution, and mobile playback.
   Verify that editing human notes
   or changing YAML formatting does not enqueue transcription, while changing
   consumed guidance does. Test target backlink exemptions and incoming-edge
   revalidation in both server and WASM paths.
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
