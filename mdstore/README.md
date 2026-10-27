# mdstore

`mdstore` is a Rust daemon for a Git-tracked Markdown knowledge repository. It
validates repository-defined schemas, exposes hashline-safe atomic edits, keeps
adjacent binary embedding sidecars, and serves exact, vector, graph-assisted,
and reranked search over MCP.

Markdown, root `config.yaml`, and directory `schema.md` files are canonical.
Adjacent `*.mdstore` embedding files are disposable and ignored by Git. No
repository `.mdstore/` directory is needed; daemon state lives in Git's private
directory.

```gitignore
*.mdstore
```

## Clients

The daemon serves document APIs; it does not host a UI or interpret app definitions.
Use MCP directly or run the separate [web interface](../webui/README.md).
`cargo build --manifest-path mdstore/Cargo.toml` needs only Rust dependencies.


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

The daemon does not authenticate requests or restrict Host/Origin headers.
Deploy it on a trusted network behind a firewall or a reverse proxy that controls
access. `server.listen` can bind a private interface directly.

### Markdown validation

Markdown style validation uses [rumdl](https://github.com/rvben/rumdl). A template
references a tracked config file, resolved relative to the template directory:

```starlark
markdown("rumdl.toml")
```

The file uses rumdl's native TOML configuration:

```toml
[global]
enable = ["MD012", "MD009", "MD047"]

[MD012]
maximum = 1
```

Without an `enable` list, rumdl's default rule set applies. Use native global
settings, rule sections, Markdown flavor, exclusions and per-file ignores.
Patterns are relative to the config file's directory. Both `rumdl.toml` and
`.rumdl.toml` are supported, including shared references such as
`markdown("../rumdl.toml")`. A leading slash, as in
`markdown("/rumdl.toml")`, resolves from the repository root. Paths cannot escape
the repository or access the host filesystem.

Config files are included in the validation snapshot. Editing one uses
`allow_config_edits` and revalidates every document governed by a template that
references it. Offline validation requests any affected source not yet cached.
The same in-memory TOML configuration is used on the server and in WASM; it does
not discover files from the host filesystem. `extends`, `.editorconfig`, and
external code-block tools are unsupported and rejected. Keep referenced config
files self-contained.

Diagnostics include the rule ID, document path and one-based line number. Checks
run at startup and on changed documents or documents affected by a template
change. Invalid batches change nothing; there is no automatic formatting.
MD057 (filesystem link existence) is excluded from the default set and cannot be
enabled: mdstore checks cross-document links against the proposed repository
snapshot, identically online and offline. Structural, schema, relationship and
Starlark callback validation remain separate from Markdown linting.

### Directory templates

Place `schema.md` beside the documents it governs. The nearest ancestor
template applies recursively and replaces its parent. Use directories such as
`tasks/v1/` and `tasks/v2/` for incompatible formats; no version registry is needed.
Templates are readable through `get_page`, protected from `apply_edits`, and
excluded from search and embeddings.

Markdown supplies instructions and examples. Only top-level fences labelled
exactly `starlark` execute; their contents form one module in document order.
See the complete [task template](examples/tasks/v1/schema.md). Tasks use their first H1 as the title, without a duplicate frontmatter field. Callbacks can read `doc.title` (empty if no H1 exists). Search metadata defaults to the first H1, then the path, unless an explicit title projection supplies a value.

The web preview renders frontmatter below the H1 as document properties. Compiled template enums become badges; state/status and tags have sensible fallback formatting. Empty values and duplicate titles are hidden, other properties can be expanded, and invalid YAML stays visible as source with an error. Editing always preserves the original YAML.

Declarations register rules checked by Rust:

| Declaration | Purpose |
| --- | --- |
| `frontmatter(**fields)` | Define fields; unknown fields are rejected. `fields={...}` supports arbitrary names; `allow_extra=True` permits extra fields. |
| `string(...)`, `integer(...)`, `boolean(...)`, `enum(values, ...)`, `list_of(item, ...)` | Field constructors with `required=False` and `nullable=False`. Strings accept length/pattern constraints, integers minimum/maximum, and lists `unique`/`min_items`. |
| `field(schema, required=False)` | Construct a field from JSON Schema, including nested objects. |
| `section(heading, ...)` | Require or constrain a section. Defaults to H2; `parent=[...]` addresses previously declared parent sections. |
| `dated_list(order="ascending", min_items=1, allow_equal_timestamps=True)` | Unordered entries starting with RFC3339 timestamps and explanations, ordered by instant. |
| `filename(pattern, serial_scope=[])` | Match the complete repository-relative path; named regex captures define serial scopes. |
| `scope(exclude=[])` | Exclude paths relative to this template directory, falling back to the parent schema. |
| `metadata(**pointers)` | Project frontmatter fields through JSON pointers. |
| `markdown(config)` | Reference a tracked `rumdl.toml`, relative to the template. |
| `links(markdown=True, wiki=[])` | Select link syntax; wiki regexes require a named `target` capture. |
| `relation(name, selector, reciprocal=None)` | Select authored links or frontmatter arrays; reciprocal facts must be authored in the same batch. |
| `structure(level=None, order="unrestricted", additional_sections=True)` | Set root heading policy before declaring sections. |
| `preamble(**rules)` | Constrain content before the declared sections. |

Wiki targets first resolve relative to the source document's directory. If no
local document matches, they fall back to repository paths, then unique short
names. Explicit `./` and `../` targets stay relative; paths cannot escape the
repository. For example, `[[meeting]]` in `work/page.md` prefers
`work/meeting.md` over `personal/meeting.md` or `meeting.md`.

Section rules include `required`, `nonempty`, `include_subsections`, and `content`
(`paragraphs`, `list`, `table`, `code`, `blockquotes`, `empty`, or `dated_list()`).
`paragraphs`, `words`, `characters`, and `list_items` accept `minimum`/`maximum`
bounds. A plain `list` rule accepts `ordered`, `minimum_items`, `item_pattern`, and
`date_order` for YYYY-MM-DD prefixes. Declared sections cannot repeat among
siblings. Timeline entries may contain continuation paragraphs and nested lists.

Custom rules use `validate(callback)` for a document or
`validate_change(callback)` for an atomic edit's `before, after` snapshots.
Creation supplies `before=None`; deletion supplies `after=None`. Call
`require(condition, message, field="state")` or `require(..., at=entry)` to reject
with a source location. Callbacks return `None`; schema failures skip document
callbacks, and document failures prevent change validation.

Documents expose immutable `path`, `text`, `frontmatter`, `links`, `line`, and
`sections`. Sections are keyed by unique heading text; ambiguous names are omitted, so direct
lookup fails instead of selecting another section. Sections expose `text`, `level`, `line`, and `entries`. Entries expose
`timestamp` (RFC3339 or `None`), `text`, `links` (destination strings), and `line`.
YAML frontmatter must be a mapping with unique keys; values are never coerced.
Schema references may resolve within the schema itself; external retrieval is forbidden.

Modules are compiled and frozen at activation. Each evaluation has limits of
100,000 ticks, 16 MiB of Starlark heap, and 64 stack frames; source is limited to
1 MiB. No imports, filesystem, network, clock, or random APIs are available.
These interpreter limits are best-effort, not process isolation.

Every atomic edit validates the full proposed corpus and its transitions before
writing. External Git imports and startup validate the final snapshot against
its templates; they do not replay historical transitions.

On creation, the single `path` argument accepts `{serial}` or `{serial:03}`.
The placeholder must occupy the filename pattern's named `serial` capture.
Under the repository lock, allocation chooses the greatest current serial in
`serial_scope` plus one, including other creates in the batch. Padding is a
minimum width, capped at 12. Deleting the highest serial permits reuse. Unknown
or repeated placeholders and placeholders on other edit operations are rejected.
Allocation changes only the path, and retries consult durable receipts first.
Read the parent directory after a successful edit to discover allocated paths.

`get_page` returns the governing schema path in `template`, also for proposed
paths with `content: null`. Read that path separately for its rules and original
Markdown. Template updates are authored externally and activated through
validated Git synchronization.

## Local durability and background Git synchronization

An accepted edit is committed locally before it returns. It never waits for a
network request, including when retrying an already-applied batch. The MCP
`apply_edits` response contains only `{"success": true}` or `{"errors": [...]}`.
Errors carry a message and, for validation findings, a repository-rooted `location`
such as `/notes/page.md:5`. Without a line number, the path is `/notes/page.md`.
An optional `schema` uses the same `/schema.md:12` syntax for the governing rule.
Absent schema locations are omitted; there is no separate `line` field. Results
use `structuredContent` without a duplicate text copy; failures set `isError: true`. Embedding rebuilds and Git synchronization are independent
background activities.

With `git.push: true`, the running daemon synchronizes at startup, after edits,
and every 30 seconds while healthy. Failures retry with exponential backoff from
1 second to a 5-minute ceiling, even if no further edits arrive. The existing
`git.push_timeout_seconds` bounds each network operation. Network failures are
recorded in status and do not block local writes. `git.push: false` disables the
worker.

The worker fetches the configured branch into a private ref. A fast-forward
candidate's complete tree, configuration, templates, and sidecar ignore rules
must validate before the live branch, checkout, or published state changes.
Invalid incoming trees leave the accepted state untouched. Valid listener
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

## Running the daemon

```sh
mdstore --root /path/to/brain serve
```

The daemon exposes `/mcp` for document operations and `/health` for operational
status. `get_page` accepts `/` or a directory path ending in `/` to list direct
children of the published document tree. Use `/notes/**` for all descendants
(files and directories), or `/notes/**/` for descendant directories only; root
forms are `/**` and `/**/`. Subtree listings exclude the starting directory,
are sorted and deduplicated, and return the complete listing without truncation.
These paths also work in batch reads. Directory reads return only newline-
separated paths, with trailing `/` for directories. No hashes, revision,
repository ID or configuration flags are included. Default file reads return
plain text containing the path, schema path and hashline content. Raw file reads
return structured `path`, `template` and `content` fields; missing files have
`content: null`. Returned paths start with `/` and are rooted at the repository
root. Reads and edits accept paths with or without the leading `/`. Empty
directories are not persisted by Git. Clients traverse directory lists and read
sources to build inventories, content fingerprints, validation baselines and
previews locally. The web editor reads edit permissions from `config.yaml`.
`apply_edits` always validates changes before committing.

The MCP endpoint supports protocol version `2025-06-18`. Older `2025-03-26`
requests are no longer accepted; initialization advertises the supported
version.

An edit request uses `LINE:HASH` anchors returned by `get_page`. Hashes are four
hexadecimal characters (16 bits of SHA-256). Each hash covers the line and its
immediate previous and next lines, with length-prefixed fields and empty strings
for missing neighbors. Trailing whitespace is significant. The numbered line
must match: stale anchors fail without relocation or merging. All operations in
a batch validate against the same pre-edit snapshot.

`apply_edits` groups operations by path; individual operations have no `path`:

```json
{
  "edit_summary": "Update notes",
  "edits": {
    "/notes/example.md": [
      {"op": "replace", "anchor": "10:abcd", "content": "Updated paragraph."},
      {"op": "insert_after", "anchor": "20:1234", "content": "Another paragraph."}
    ]
  }
}
```

The path map and every operation array must be non-empty. Paths accept an
optional leading `/`, but aliases such as `/notes/a.md` and `notes/a.md` in the
same request are rejected. The whole request remains atomic across files.
The former flat operation array is not accepted.


`get_page` returns page content once as a plain-text MCP content block, with a
path/schema header and hashline prefixes by default. Append `:10` to read one
line or `:10-20` to read an inclusive line range. Separate line-number arguments
are not accepted. Raw file reads, searches and edit results use
`structuredContent` with an empty `content` array. No response includes a
serialized JSON copy for older clients. File reads omit revision, file hash,
existence flags, derived metadata and relations. `template` is the
repository-rooted schema path, such as `/schema.md` (or `null`); read that path
separately for its instructions and rules. Use `format: "raw"` for structured
output containing exact full-page source, including line endings, when preparing
`replace_page` or `delete_page`. Raw reads support files only and cannot specify
a line window. The schema is always a path; the web editor compiles property
schemas locally from the schema sources it reads.

`path` also accepts a non-empty array, for example `{"path": ["/alice.md",
"/bob.md:10-20"]}`. Results preserve input order, including repeated paths.
`format` applies to every path. Hashline batches return one text block per path,
including plain directory lists. Raw file batches return `structuredContent:
{"results": [...]}` without a text copy. Single-path calls keep their existing
response shape. An invalid read rejects the complete batch; a missing Markdown
page remains an ordinary missing-page result.
Example edit:

```json
{
  "edit_summary": "Link Alice and Bob",
  "edits": [
    {
      "op": "insert_after",
      "path": "people/alice.md",
      "anchor": "12:89f2",
      "content": "- [Bob](bob.md)"
    },
    {
      "op": "insert_after",
      "path": "people/bob.md",
      "anchor": "9:ce55",
      "content": "- [Alice](alice.md)"
    }
  ]
}
```

All anchors in a request resolve against the same pre-edit snapshot. Stale,
ambiguous, or overlapping edits fail before any worktree file changes.

The MCP endpoint is `/mcp`; health and indexing coverage are available from
`/health`. Access control belongs to the deployment firewall or reverse proxy.
Startup reuses valid sidecars and rebuilds only missing or stale vectors; the explicit `reindex` command forces a
complete rebuild.

### Editing templates and configuration through the API

Both are read-only by default. Enable the corresponding capabilities in the
repository's `config.yaml` and restart the daemon:

```yaml
server:
  allow_template_edits: true
  allow_config_edits: true
```

These permissions apply to every caller that can reach the daemon. The UI
discovers them from root-directory `get_page` reads. Root configuration cannot be deleted. Template changes
compile the proposed Starlark and validate the entire proposed corpus; documents
can be updated in the same atomic batch to satisfy new rules. Transition checks
from both the old and proposed templates remain enforced. Configuration changes
must parse and pass configuration and corpus validation before publication.
Listener changes return `restart_required: true`; the running listener remains
active until the daemon is restarted.

### Incremental validation

Edit submission reuses the daemon's last validated snapshot.
Unchanged documents retain their parsed content and authored relation edges.
Content and template checks run for edited documents and documents whose nearest
template changed, including changes to template inheritance. Existing transition
checks still run against before/after content, and reciprocity is checked on the
resulting graph so removing a backlink can invalidate an unchanged document.

Adding or removing document paths re-resolves authored targets across the graph:
even adding a document can make an existing short-name target ambiguous. This
reuses cached parsing and does not rerun unchanged document policies. Startup,
external snapshot activation, and explicit full validation retain complete
validation as the baseline.

### Portable validation

The `mdstore::validation` library API exposes parsing, compiled templates, full and
incremental corpus validation without server features.
Independent clients can link it with `default-features = false`.

Templates may declare `scope(exclude=["overview.md"])`. Patterns are relative to
the template directory. Excluded documents inherit the nearest matching parent
schema; they do not bypass ordinary Markdown parsing or link validation.
