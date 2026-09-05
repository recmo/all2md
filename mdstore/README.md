# mdstore

`mdstore` is a Rust daemon for a Git-tracked Markdown knowledge repository. It
validates repository-defined schemas, exposes hashline-safe atomic edits, keeps
adjacent binary embedding sidecars, and serves exact, vector, graph-assisted,
and reranked search over MCP.

Markdown, root `config.yaml`, and directory `template.yaml` files are canonical.
Adjacent `*.mdstore` embedding files are disposable and ignored by Git. No
repository `.mdstore/` directory is needed; daemon state lives in Git's private
directory.

```gitignore
*.mdstore
```

## Repository configuration

Every served repository tracks a root `config.yaml` with operational settings
only. Document rules belong exclusively in directory templates. Both kinds of
configuration are readable but cannot be changed through `apply_edits`.

```yaml
documents:
  include: ["**/*.md"]
  exclude: ["archive/**"]
chunking:
  target_tokens: 400
  overlap_percent: 15
  max_chars: 2000
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
git:
  push: true
  remote: origin
  push_timeout_seconds: 30
server:
  listen: 127.0.0.1:3131
```

### Markdown validation

The `markdown` checks in a directory template are independently configurable and disabled when
omitted. They impose no required frontmatter, section names, or initial heading
level. `heading_increment` rejects jumps such as H2 to H4; decreasing levels is
allowed. `fence_language` requires a nonempty info string, not a fixed language
list. `nonempty_links` checks parsed Markdown link destinations.

Whitespace and line-length checks cover the body, excluding fenced and indented
code blocks. Exactly two trailing spaces on a nonblank line are allowed for
Markdown hard breaks. Line length counts Unicode characters, not bytes; CRLF
and LF endings are accepted. `final_newline` covers the entire nonempty file.

Failures include the rule name, page path, and one-based source line. Checks run
at startup, on `validate`, and against the complete proposed tree before an edit
is committed. Incoming template changes are validated against every selected page before activation. Invalid batches change nothing; there is no auto-formatting.
These are explicit checks on CommonMark parsing, not a guarantee that every
typo is rejected: unmatched emphasis and undefined reference syntax can still
be ordinary text, and link fragments are not checked against headings.

### Directory templates

Place `template.yaml` in the directory whose documents it governs. It applies
recursively; the nearest ancestor template replaces its parent completely,
without merging. Templates are ordinary versioned YAML files, not sidecars.
All template definitions are checked, including those in empty directories.
There are no additional global document-validation rules in `config.yaml`.

Example `people/template.yaml`:

```yaml
instructions: Describe established facts, not speculation.
examples: []
frontmatter:
  type: object
  required: [name]
  properties:
    name: {type: string}
metadata:
  display_name: /name
markdown:
  closed_fences: true
  nonempty_headings: true
  no_trailing_whitespace: true
  final_newline: true
links:
  markdown: true
relations:
  - name: related
    reciprocal: related
    selector: {kind: markdown_links}
structure:
  level: 2
  order: enforced
  additional_sections: false
preamble:
  words: {maximum: 10}
sections:
  - heading: Summary
    instructions: Summarize the person in one concise paragraph.
    rules:
      required: true
      content: paragraphs
      paragraphs: {minimum: 1, maximum: 1}
      words: {maximum: 100}
  - heading: Timeline
    instructions: Record meaningful interactions, newest first.
    rules:
      required: true
      list:
        ordered: false
        minimum_items: 1
        date_order: descending
```

Each section can have its own `instructions`, `examples`, `rules`, `structure`,
and nested `sections`. Heading names are exact, siblings must be unique, and
duplicate authored sections are rejected. `rules.required` defaults to false.
`structure.level` defaults to one level below the parent (H1 at the root),
`order` defaults to `unrestricted`, and `additional_sections` defaults to false.
Initial headings above the root section level, such as an H1 document title
before H2 sections, belong to the preamble. Structural headings must be outside
lists and blockquotes. Extra sections, when allowed, have unconstrained content.

`preamble` uses the same content rules as sections. `content` may be
`paragraphs`, `list`, `table`, `code`, `blockquotes`, or `empty`; omission allows
any block type. `nonempty: true` requires non-whitespace text. `paragraphs`,
`words`, `characters`, and `list_items` each accept optional `minimum` and
`maximum`. Paragraphs are AST paragraphs, list-item counts include nested items,
words are whitespace-delimited, and characters are Unicode scalar values of
text with Markdown delimiters removed (not browser layout or grapheme counts).
Block boundaries separate words; inline emphasis does not. Raw HTML is not
rendered. Limits cover a section's own content unless
`include_subsections: true`. `rules.list` uses the dated-list and item-pattern
rules documented below. Instructions and examples are advisory, never evaluated
by an LLM or used as blocking checks.

`get_page` includes `exists` and `template: {path, definition}` for Markdown.
For a proposed, in-scope Markdown path it returns `exists: false`, empty content,
and the applicable template, so agents can discover requirements before writing.
Read the template itself through `get_page("people/template.yaml")` for hashline
text. The same information is available through `mdstore get`.

Templates are read-only through `apply_edits`, including creation and removal.
Template editing/approval is not implemented. An externally committed template
change is activated only if full-corpus validation succeeds. Direct untracked
Markdown and template additions are quarantined by repository recovery rather
than becoming an alternate source of authored state; sidecars remain disposable
and Gitignored. Invalid edits reject the complete batch and include the template
path, source line, and relevant section guidance in findings.

Template `frontmatter` is an inline JSON Schema; `metadata` maps output names to
frontmatter JSON pointers. `markdown` selects deterministic style checks.
`links` configures Markdown/wiki syntax, and `relations` selects authored links
or frontmatter relations with optional reciprocal requirements. These settings
use the nearest template, just like structure: a nested template replaces all
parent policy. Reciprocal facts must still be authored atomically by callers;
the daemon never generates them. `rules.list` supports `ordered`,
`minimum_items`, `item_pattern`, and `date_order: ascending|descending`.
Dates must be valid literal YYYY-MM-DD prefixes, with equal dates allowed.

## Local durability and background Git synchronization

An accepted edit is committed locally before it returns. It never waits for a
network request, including when retrying an already-applied batch. The response's
`push` state describes current replication, not edit success. Embedding rebuilds
and Git synchronization are independent background activities.

With `git.push: true`, the running daemon synchronizes at startup, after edits,
and every 30 seconds while healthy. Failures retry with exponential backoff from
1 second to a 5-minute ceiling, even if no further edits arrive. The existing
`git.push_timeout_seconds` bounds each network operation. Network failures are
recorded in status and do not block local writes. `git.push: false` disables the
worker. `mdstore push` explicitly attempts one synchronization immediately.

The worker fetches the configured branch into a private ref. A fast-forward
candidate's complete tree, configuration, templates, and sidecar ignore rules
must validate before the live branch, checkout, or published state changes.
Invalid incoming trees leave the accepted state untouched. Valid listener/auth
changes are staged in private Git state and reported as requiring restart.
Restart revalidates and activates the staged fast-forward before binding the
new listener. If local history advanced meanwhile, the stale stage is discarded
without overwriting local work; synchronization must reconcile the new state. Divergent histories
block further writes until explicitly reconciled; the daemon never merges or
rewrites history. A subsequent sync clears the divergence block once the
histories are compatible again.

Network operations run outside the edit lock. Pushes target a captured commit,
so a newer edit remains pending if it arrives during a push. Short local
activation/publication steps still use the repository lock. The daemon owns the
live checkout: make external changes in another checkout and publish through Git.

`status` and `/health` expose `replication.pending_commits`,
`replication.last_success` (Unix seconds), and `replication.last_error`, separately
from vector coverage. Progress reflects the last observed remote state, not a
live remote query. Last success/error survive daemon restarts and are scoped to a fingerprint of
the resolved fetch URL, push URL, and destination branch. Changing the destination
invalidates the previous progress report; pending commits are reconstructed from
destination-specific Git acknowledgements, never inferred from an old upstream. Replication metadata is private Git state.

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
mdstore --root /path/to/brain get config.yaml
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
