# mdstore

`mdstore` is a Git-backed Markdown daemon for validated search and editing over MCP. Documents, root `config.yaml`, and directory `schema.md` files are canonical; templates define document structure, relationships, link syntax, and change rules. Embedding sidecars are disposable and must be ignored by Git. Build with `nix develop .#mdstore --command cargo build --manifest-path mdstore/Cargo.toml`, then run `mdstore --root /path/to/repository serve`. Configure retrieval, Git synchronization, and the listen address in `config.yaml`; deploy behind trusted network access controls.

MCP exposes `read`, `search`, and `edit`. Reads return a snapshot commit and numbered hashline anchors. Edits use those anchors; atomic moves additionally require `expected_commit`. Edits address original paths, then moves apply simultaneously, supporting chains, swaps, and explicitly vacated destinations. The server preserves document identity while rewriting Markdown/wiki links and rebasing relative assets, leaving labels, fragments, provenance, URLs, and code examples intact. Ambiguous rewrites, collisions, and invalid final schemas reject the batch. Successful edits, moves, backlinks, and deletions publish in one Git commit with retry receipts. See the MCP tool schemas for request details and the [task template](examples/tasks/v1/schema.md) for schema examples.

## ML workers and file resources

All search inference runs through `ml-worker`: document chunks are queued automatically
at startup and after edits; query embeddings and reranking use the same pinned
local recipes. The ZeroEntropy hosted API integration has been removed. Its API-key
and endpoint settings are unsupported and rejected by configuration validation. Set the same
`MDSTORE_WORKER_TOKEN` on the daemon and worker, then run:

```sh
ml-worker --server http://mdstore:3131 --recipes text-embeddings-zembed-1-v1 text-rerank-zerank-2-v1
```

Retrieval jobs and results live under Git's private `mdstore/retrieval` directory.
They survive daemon restarts without adding generated documents or Git commits.
Validated document vectors enter the live search index as individual chunks finish.
Complete pages are saved as ignored `.mdstore` sidecars. Changed/deleted chunk
inputs and expired worker attempts reject completion. Query/ranking requests take priority, followed by explicitly declared
jobs and then background document indexing. Interactive requests expire
after `provider.request_timeout_seconds` (default 300). If workers are unavailable,
search reports degraded retrieval and retains exact search. Pending/failed jobs
appear in `/health`; failed retrieval jobs support the existing retry operation.
`provider.batch_size` controls document batches (default 1, maximum 256)
so interactive work can run between document chunks.
Completed JSON results are stored separately from queue metadata; cache keys include
the pinned model revision. Models and 2,560-dimensional vectors are fixed by the
worker recipes. `chunking.max_chars` must be at most 32,768.
Remove old `provider.base_url`, `api_key_env`, `embedding_model`, `rerank_model`,
and `dimensions` settings from existing configurations; old hosted sidecars
are rebuilt automatically with the local embedding-space identity.

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
