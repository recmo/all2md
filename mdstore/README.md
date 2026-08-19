# mdstore

`mdstore` is a Rust daemon for a Git-tracked Markdown knowledge repository. It
validates repository-defined schemas, exposes hashline-safe atomic edits, keeps
adjacent binary embedding sidecars, and serves exact, vector, graph-assisted,
and reranked search over MCP.

Markdown and `.mdstore/` configuration are canonical. `*.mdstore` embedding
files are disposable and must be ignored by Git. Because the configuration
directory has the same suffix, repositories should use both rules:

```gitignore
*.mdstore
!.mdstore/
```

## Repository configuration

Every served repository must track `.mdstore/config.yaml`. Document fields,
section names, relation types, and reciprocity are configuration rather than
Rust conventions. A minimal example is:

```yaml
documents:
  include: ["**/*.md"]
  exclude: ["archive/**"]

schemas:
  - include: "people/**/*.md"
    schema: ".mdstore/schemas/person.json"

metadata:
  display_name: /name
  tags: /tags

links:
  markdown: true
  wiki:
    - '\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]'

relations:
  - name: mentions
    reciprocal: mentioned_by
    selector:
      kind: markdown_links
      include: "people/**/*.md"
      section: Mentions
      syntax: markdown
  - name: mentioned_by
    reciprocal: mentions
    selector:
      kind: frontmatter
      array_pointer: /backlinks
      target_pointer: /target

chunking:
  target_tokens: 400
  overlap_percent: 15
  max_chars: 2000
  exclude_sections: []
  context_pointers: [/name]

search:
  limit: 10
  candidates: 30
  rrf_k: 60
  graph_weight: 0.15

provider:
  base_url: https://api.zeroentropy.dev/v1
  api_key_env: ZEROENTROPY_API_KEY
  embedding_model: zembed-1
  rerank_model: zerank-2
  dimensions: 1280
  batch_size: 64
  request_timeout_seconds: 30

git:
  push: true

server:
  listen: 127.0.0.1:3131
  bearer_token_env: MDSTORE_BEARER_TOKEN
```

The example `mentioned_by` representation is deliberately configured; mdstore
does not create it. A batch adding `mentions` must also contain the matching
`mentioned_by` edit or whole-tree validation rejects the batch.

## CLI

Run `serve` first. Every other command is an HTTP client of that daemon, using
`server.listen` and `server.bearer_token_env` from the repository configuration.
Use global `--daemon-url` or `MDSTORE_URL` when the daemon was started at an
overridden address.

```sh
mdstore --root /path/to/brain validate
mdstore --root /path/to/brain serve
mdstore --root /path/to/brain search "query" --variant "caller expansion"
mdstore --root /path/to/brain get people/alice.md
mdstore --root /path/to/brain get .mdstore/config.yaml
mdstore --root /path/to/brain apply --file edits.json
mdstore --root /path/to/brain reindex
mdstore --root /path/to/brain status
mdstore --root /path/to/brain push
```

An edit request uses `LINE:HASH` anchors returned by `get_page`:

```json
{
  "edit_summary": "Link Alice and Bob",
  "edits": [
    {
      "op": "insert_after",
      "path": "people/alice.md",
      "anchor": "12:a3",
      "content": "- [Bob](bob.md)"
    },
    {
      "op": "insert_after",
      "path": "people/bob.md",
      "anchor": "9:f1",
      "content": "- [Alice](alice.md)"
    }
  ]
}
```

All anchors in a request resolve against the same pre-edit snapshot. Stale,
ambiguous, or overlapping edits fail before any worktree file changes.

The MCP endpoint is `/mcp`; health and indexing coverage are available from
`/health`. A configured bearer token protects both endpoints, and listening
beyond loopback is refused without one. Startup reuses valid sidecars and
rebuilds only missing or stale vectors; the explicit `reindex` command forces a
complete rebuild.
