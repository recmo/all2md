# mdstore

`mdstore` is a Git-backed Markdown daemon for validated search and editing over MCP. Documents, root `config.yaml`, and directory `schema.md` files are canonical; templates define document structure, relationships, link syntax, and change rules. Embedding sidecars are disposable and must be ignored by Git. Build with `nix develop .#mdstore --command cargo build --manifest-path mdstore/Cargo.toml`, then run `mdstore --root /path/to/repository serve`. Configure retrieval, Git synchronization, and the listen address in `config.yaml`; deploy behind trusted network access controls.

MCP exposes `read`, `search`, and `edit`. Reads return a snapshot commit and numbered hashline anchors. Edits use those anchors; atomic moves additionally require `expected_commit`. Edits address original paths, then moves apply simultaneously, supporting chains, swaps, and explicitly vacated destinations. The server preserves document identity while rewriting Markdown/wiki links and rebasing relative assets, leaving labels, fragments, provenance, URLs, and code examples intact. Ambiguous rewrites, collisions, and invalid final schemas reject the batch. Successful edits, moves, backlinks, and deletions publish in one Git commit with retry receipts. See the MCP tool schemas for request details and the [task template](examples/tasks/v1/schema.md) for schema examples.

## ML workers and file resources

All search inference runs through `ml-worker`: document chunks are queued automatically
at startup and after edits; query embeddings and reranking use the same pinned
local recipes on an Apple Silicon Mac using native MLX. Linux workers provide
audio recipes only. The ZeroEntropy hosted API integration has been removed. Its API-key
and endpoint settings are unsupported and rejected by configuration validation. Set the same
`MDSTORE_WORKER_TOKEN` on the daemon and worker, then run:

```sh
ml-worker --server http://mdstore:3131 --recipes text-embeddings-zembed-1-v1 text-rerank-zerank-2-v1
```

Retrieval jobs and results live under Git's private `mdstore/retrieval` directory.
They survive daemon restarts without adding generated documents or Git commits.
Validated document vectors enter the live search index as individual chunks finish.
Complete pages are saved as ignored `.mdstore` sidecars. Forced rebuilds discard
old sidecars before queuing fresh work. Changed/deleted chunk inputs and expired
worker attempts reject completion. Query/ranking requests take priority, followed
by explicitly declared jobs and then background document indexing. Interactive
jobs and cached results expire after `provider.request_timeout_seconds` (default
30). Identical requests share one pending job or completed result. Idle workers poll
every second, within the foreground wait budget. With no worker poll
or matching heartbeat in the last 60 seconds, it skips new interactive inference
and returns lexical/graph results, plus vector retrieval when a recent query
embedding is cached. Reranking is skipped unless a matching result is cached.
Each inference stage waits up to `provider.request_timeout_seconds` (default
30 seconds) before returning degraded results. The WebUI lets the daemon enforce
the search deadline; other MCP clients must allow enough time for both embedding
and reranking. Document embedding jobs remain
queued without an offline expiry and resume when workers return. Pending/failed jobs
appear in `/health`; failed retrieval jobs support the existing retry operation.

The default timeout is based on MacBook MLX batch-4 measurements: approximately
8 seconds to rerank 30 passages of 400 tokens, plus allowances of 5 seconds for
process startup/model loading, 15 seconds for queueing, and 2 seconds for polling
and network overhead. These are planning allowances, not measured tail-latency
guarantees. Embedding and reranking run sequentially, so a search can wait roughly
60 seconds in total. Set `provider.request_timeout_seconds` higher for longer
passages, larger candidate sets, or sustained queue contention. Large audio input
transfers retain their independent worker transport timeout.

Each document chunk is a separate embedding job. Workers request batches of jobs
of one recipe through `/worker`, choosing how many jobs to claim per recipe.
The MLX worker claims up to four embedding jobs; a reranking job already contains
its candidate passages. It groups inputs into model calls of at most four items
and 2,048 padded tokens. Longer individual inputs run
alone. Every job keeps its own lease, result, and retry state. Interactive jobs
have priority over declared derivations and background indexing.
Completed JSON results are stored separately from queue metadata; cache keys include
the pinned model revision. Models and 2,560-dimensional vectors are fixed by the
worker recipes. `chunking.max_chars` must be at most 32,768.

Declare other derived outputs in root `config.yaml` under `derivations`; see the
[ML worker](../ml-worker/README.md) for recipes, model versions, and configuration.
Set `MDSTORE_WORKER_TOKEN` on the daemon and worker to a separate worker credential.
Workers claim leased jobs through `/worker`, fetch only assigned inputs, and submit
assigned outputs. The daemon validates and commits publications, fences stale
leases, and protects generated files from ordinary edits and moves.

Repository files also have URLs at `/<path>`. GET streams content and supports
conditional/range requests for assets. PUT and DELETE require `If-Match` or
`If-None-Match: *`; text writes use the same validation and atomic commit path as
MCP edits. `Accept: application/vnd.mdstore.page+json` requests WebUI metadata.
`/health` includes job status; `/mcp/events` streams repository and job invalidations.
The trusted-network access model remains the same as the MCP listener.

## External embeddings (msgvault)

`POST /v1/embeddings` returns OpenAI-compatible float embeddings through the
same durable pull queue and resident MLX models. Set `MDSTORE_EMBEDDINGS_TOKEN`
on the daemon and send it as a bearer token. Use a dedicated client credential,
separate from `MDSTORE_WORKER_TOKEN`; the embeddings credential cannot access
other daemon routes. The endpoint is disabled without this credential.

Use model `zeroentropy/zembed-1-embedding`, dimensions `2560`, and
`encoding_format: "float"` (dimensions and encoding may also be omitted).
`input` accepts a string or an ordered array of 1–4 strings. Every input must
start with `query: ` or `document: `, and all inputs in a request must have the
same role. Only this first role prefix is removed. The worker applies the pinned
model's query/document chat formatting once. The remaining texts must be
nonempty, with at most 32,768 total characters per request.

External texts live only in the private queue under `.git/mdstore/retrieval`.
They create no corpus documents, sidecars, or search-index entries. Results keep
input order and use the worker's validated, normalized 2,560-dimensional vectors.
Retries with identical model, role and ordered texts reuse pending work and
completed results. Completed external results expire after a day. Background
jobs survive client disconnects, daemon restarts and worker downtime until they
complete; expired worker leases are reclaimed by the existing retry machinery.
A full mdstore reindex does not cancel external jobs.

Queries take the same priority as mdstore interactive inference and wait at most
three seconds (or fail immediately when no worker has polled recently). Query
jobs have a five-minute lifetime; completed results remain usable offline for a
day. Documents wait at most 25 seconds per HTTP request, then return retryable
503 with `Retry-After: 5`. HTTP cancellation never cancels the queued document
work. Background retrieval claims contain at most four texts across clients;
existing larger internal requests still run alone. No additional inference
process or model loading is involved.

Msgvault v0.23.0 already supports this role distinction through its existing
`[vector.embeddings]` settings:

```toml
[vector]
enabled = true
backend = "sqlite-vec"

[vector.embeddings]
endpoint = "http://10.77.0.10:3131/v1"
api_key_env = "MSGVAULT_EMBEDDINGS_TOKEN"
model = "zeroentropy/zembed-1-embedding"
dimension = 2560
document_prefix = "document: "
query_prefix = "query: "
batch_size = 4
max_input_chars = 2048
timeout = "30s"
max_retries = 1

[vector.embed.schedule]
cron = "*/10 * * * *"
run_after_sync = false
```

Keep the embedding token in the deployment's secret environment files, never in
Nix source or the store. HTTP tracing records only method/path, without bearer
headers or message bodies. Msgvault owns its SQLite message vectors and its
embedding schedule/checkpoints. Its HTTP client retries and its scheduled builds
resume missing messages; no caller-side queue is needed. Before enabling the
schedule, build and search a small sample and verify worker recovery. Configure
Nix only after this all2md change merges, pinning that merged main commit without
local all2md patches. Hybrid query failure uses the msgvault keyword fallback maintained in the Nix
package patch; explicit `mode=fts` remains available during initial indexing.

Live ZeroEntropy reranking remains separate: msgvault's hybrid engine sends
candidates directly from fusion to hydration and has no external reranker hook.
Adding one is a follow-up; the embeddings endpoint does not rerank messages.

After deployment, compare fresh and cached searches for the sample, check the
returned message relevance, then disconnect the MacBook and verify bounded
hybrid fallback, usable keyword search, and automatic background recovery after
reconnection. Start archive-wide backfill only after that sample passes.
