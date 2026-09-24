# mdstore

`mdstore` is a Rust daemon for a Git-tracked Markdown knowledge repository. It
validates repository-defined schemas, exposes hashline-safe atomic edits, keeps
adjacent binary embedding sidecars, and serves exact, vector, graph-assisted,
and reranked search over MCP.

Markdown, root `config.yaml`, and directory `template.md` files are canonical.
Adjacent `*.mdstore` embedding files are disposable and ignored by Git. No
repository `.mdstore/` directory is needed; daemon state lives in Git's private
directory.

```gitignore
*.mdstore
```

## Web workspace

The daemon serves a SvelteKit SPA at its root URL. Build it before compiling Rust
to embed the UI:

```sh
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --version 0.2.121 --locked
pnpm --dir webui install --frozen-lockfile
pnpm --dir webui build
cargo build --manifest-path mdstore/Cargo.toml
mdstore --root /path/to/brain serve
```

Open the configured daemon address (default `http://127.0.0.1:3131/`). Nix builds
and embeds the frontend automatically; production still needs only the Rust binary.
See [frontend development and tests](../webui/README.md).

A plain Cargo build/test does not require Node or frontend assets. Without a built
UI, the daemon serves a setup page and the API remains available. Packaging can
set `MDSTORE_WEB_DIST` to a built asset directory; an invalid explicit path fails.

Browse documents and templates, or search using the MCP `search` tool,
including optional query variants and degradation reporting. Reads and submissions
use MCP `get_page` and `apply_edits`. Pierre CodeView edits source in Code view;
Rendered view shows local Markdown with Pierre-highlighted code blocks.
Switching views preserves the editor's undo history and selection. Connection credentials are configured on the Settings page.
Raw HTML is escaped, rendered output is sanitized, and images display descriptions
without external requests. Relative document links open within the workspace.

Documents visited, template instructions, original draft bases, edits, and commit
summaries persist in localStorage, separated by repository. “Make library available
offline” caches all listed documents; the service worker caches the application
shell for offline reloads on localhost or HTTPS. Offline search covers cached text.
Tokens stay in memory. The local-cache control can erase cached documents and drafts;
cache failures are reported and drafts can be exported.

Drafts for multiple documents form one atomic change set. Add a summary, validate
using the API, then submit. Reconnection never submits automatically. Submission
rechecks the complete corpus, transitions, and original source under the repository
lock; local commit success is separate from Git replication.

Full `get_page` reads additionally return exact source in `text`; line-window reads
keep the existing hashline content. `replace_page` accepts `path`, `base` (the exact
original source), and `content` (the full replacement). Concurrent changes reject the
edit instead of overwriting them; line endings and final newlines are preserved.
New documents use `create_page`. The UI uses explicit paths.

Authenticated `/ui/documents` supplies paths and the cache namespace. `/ui/validate`
accepts the same request as `apply_edits` and runs preparation and validation without
writing documents or commits. There is no rendering endpoint. The MCP tool allowlist
remains unchanged.

When bearer authentication is configured, the shell asks for the token. Data
endpoints require it; previously cached documents remain available offline.
Same-origin browser requests are allowed and cross-origin requests rejected.
Without a token, only localhost/loopback Host headers are accepted.

The document tree context menu provides **New folder here**, **Rename**, and
**Move**, and **Delete**. Deleting a folder stages removal of its descendants.
Deletions are reviewed and validated in Submit before committing; Undo deletion
restores the last deletion until another staged edit is made or the page reloads.
Drag files or folders onto another folder to move them; double-click
a tree item to rename it in place (Enter to save, Escape to cancel). Right-click empty tree space to create a document or folder at the root.
The virtual Search, Settings, and Submit rows open workspace pages in the right pane;
Settings contains the connection and offline-cache controls.
Submit contains the saved change description, per-file diffs, validation findings,
and the submit action. Submission requires a description, current successful
validation, and a connection; the server validates and commits the batch atomically.
Rejected submissions preserve the local changes. Staged deletions remain in the tree
with struck-through names; selecting one opens Submit. Tree badges reserve space
beside truncated names and show cache/sync status separately from validation:
green ● for valid, red × for invalid, and ? for pending or unavailable validation. Moves and
renames are staged locally, including updates to incoming Markdown links and
relative links in moved documents. Existing drafts are preserved. The client
loads uncached documents before moving; offline moves require those sources to
be cached. Empty folders persist in browser storage and enter Git when populated.
Validation checks the complete staged operation before submission.

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

For a private reverse proxy such as Tailscale Serve, keep the listener on loopback
and explicitly list its hostname in `server.allowed_hosts`. For example:

```yaml
server:
  listen: 127.0.0.1:3131
  allowed_hosts: ["my-machine.example.ts.net"]
```

Only exact hostnames are allowed; cross-origin requests remain blocked. The proxy
must restrict access to trusted clients. Public deployments should use bearer authentication.

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

Config files are included in the browser validation snapshot. Editing one uses
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

Place `template.md` beside the documents it governs. The nearest ancestor
template applies recursively and replaces its parent. Use directories such as
`tasks/v1/` and `tasks/v2/` for incompatible formats; no version registry is needed.
Templates are readable through `get_page`, protected from `apply_edits`, and
excluded from search and embeddings.

Markdown supplies instructions and examples. Only top-level fences labelled
exactly `starlark` execute; their contents form one module in document order.
See the complete [task template](examples/tasks/v1/template.md). Tasks use their first H1 as the title, without a duplicate frontmatter field. Callbacks can read `doc.title` (empty if no H1 exists). Search metadata defaults to the first H1, then the path, unless an explicit title projection supplies a value.

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
| `metadata(**pointers)` | Project frontmatter fields through JSON pointers. |
| `markdown(config)` | Reference a tracked `rumdl.toml`, relative to the template. |
| `links(markdown=True, wiki=[])` | Select link syntax; wiki regexes require a named `target` capture. |
| `relation(name, selector, reciprocal=None)` | Select authored links or frontmatter arrays; reciprocal facts must be authored in the same batch. |
| `structure(level=None, order="unrestricted", additional_sections=True)` | Set root heading policy before declaring sections. |
| `preamble(**rules)` | Constrain content before the declared sections. |

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
Resolved paths are returned in `touched_paths` and `fresh_hashlines`.

`get_page` returns `template: {path, definition, content}`, also for proposed paths
with `exists: false`. The definition contains declared rules; content is the
original Markdown. Template updates are authored externally and activated through
validated Git synchronization.

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

An edit request uses `LINE:HASH` anchors returned by `get_page`. Hashes are
32 hexadecimal characters (128 bits of SHA-256) and include trailing whitespace:

```json
{
  "edit_summary": "Link Alice and Bob",
  "edits": [
    {
      "op": "insert_after",
      "path": "people/alice.md",
      "anchor": "12:89f234b172385da91ba4cb0c4a7d3abf",
      "content": "- [Bob](bob.md)"
    },
    {
      "op": "insert_after",
      "path": "people/bob.md",
      "anchor": "9:ce55a9a1d046372186e540e350b2d975",
      "content": "- [Alice](alice.md)"
    }
  ]
}
```

All anchors in a request resolve against the same pre-edit snapshot. Stale,
ambiguous, or overlapping edits fail before any worktree file changes.

The MCP endpoint is `/mcp`; health and indexing coverage are available from
`/health`. A configured bearer token protects all data endpoints, and listening
beyond loopback is refused without one. Startup reuses valid sidecars and
rebuilds only missing or stale vectors; the explicit `reindex` command forces a
complete rebuild.

### Editing templates and configuration through the API

Both are read-only by default. Enable the corresponding capabilities in the
repository's `config.yaml` and restart the daemon:

```yaml
server:
  allow_template_edits: true
  allow_config_edits: true
```

These permissions apply to every caller accepted by this server's bearer-token
check (or local callers when authentication is disabled). The UI discovers them
from `/ui/documents`. Root configuration cannot be deleted. Template changes
compile the proposed Starlark and validate the entire proposed corpus; documents
can be updated in the same atomic batch to satisfy new rules. Transition checks
from both the old and proposed templates remain enforced. Configuration changes
must parse and pass configuration and corpus validation before publication.
Listener and bearer-token source changes return `restart_required: true`; the
running listener and authentication remain active until the daemon is restarted.

### Incremental validation

API validation and edit submission reuse the daemon's last validated snapshot.
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

### Client-side WASM validation

The browser executes the same Rust validation library in a Web Worker,
including the Starlark interpreter, schema checks, Markdown rules, link resolution,
and transition checks. `pnpm build` builds the WASM module and generated bindings
before bundling the SPA; Nix builds it as a separate dependency. Generated binaries
and bindings are not committed. Run `pnpm --dir webui build:wasm` before
standalone frontend type checks in a fresh checkout.

The authenticated `/ui/validation-snapshot` endpoint provides a versioned baseline
with a Git revision, source hashes, parsed document facts, relation edges, and
configuration/template sources. It does not include all document bodies. Ordinary
edits validate against that baseline without fetching unchanged document source.
Template changes request only affected missing sources and verify their hashes
before using them. Offline results remain incomplete if those sources are absent.
Changing document selection requires server validation because new globs can
include tracked files absent from the client's inventory.

The app caches the baseline, worker and WASM binary for offline use. Local results
are advisory against the cached revision; submission always runs authoritative
server validation, permissions and optimistic concurrency checks again. Revalidate
refreshes the baseline when online. The worker preserves Starlark's execution
limits and is terminated if a request exceeds 15 seconds.


### Reconciling browser drafts

Opening a staged document, visiting Submit, reconnecting, or checking server
changes fetches current source and performs a client-side three-way merge against
the draft's original base. Disjoint edits merge automatically. Conflicts preserve
both versions and the original base in local storage; the tree marks them and
Submit offers passage-by-passage Yours, Server, and manual resolutions using
Pierre diffs. Manual resolutions are saved locally and can be completed offline.
Resolving updates the exact base and triggers validation again.

Delete/modify, remote deletion, and colliding new files require explicit choices.
Moves remain staged as source deletion and destination creation; a remotely
modified source is therefore a deletion conflict, and keeping the source leaves
the destination staged as a copy. No remote rename is inferred from filenames.
Submission checks current sources again, and the server still validates and
compares bases atomically. Changes that arrive during submission are reconciled
for another review instead of silently overwriting either version.

### Starlark apps and collections

A Markdown file with `mdstore: app` in YAML frontmatter defines an app using
top-level Starlark fences. Open the file to render its views; Edit shows its source.
App files can sit beside their schema and are excluded from collection records
and the surrounding record schema. Their Starlark declarations are validated, and
editing app definitions requires template-edit permission. See
[the task planner](examples/tasks/v1/app.md) for a complete example.

`collection(name, query)` registers a plain function over an in-memory list of
immutable document records. Records expose `path`, `title` (first H1, filename
fallback), `template` (root-relative path with leading slash), `frontmatter`, and
`text` (nullable for metadata-only records). Template and app definition files are
not collection records. Queries can use comprehensions, sorting and dictionaries;
there is no query DSL, database or index. Local drafts replace baseline records;
staged deletions disappear. Missing metadata and offline freshness are displayed
explicitly. Evaluation runs in a bounded WASM worker without filesystem, network,
loaders or implicit clock access.

Views bind roles to frontmatter JSON pointers, or `title`/`path`:

- `table(name, collection, columns=[...])`: sortable SVAR DataGrid.
- `kanban(name, collection, group="/state", columns=[...], on_move="action")`:
  SVAR Kanban with drag moves and a keyboard/touch-accessible move selector.
- `gantt(name, collection, start="/start", end="/end", group=None, dependencies=None)`: interactive
  SVAR Gantt with a custom task-template extension. With no group, each document gets its own row;
  `group="/assignee"` places tasks in resource rows and stacks overlaps. Dates
  include their final day. Missing assignments appear as Unassigned; missing or
  invalid dates remain listed below the chart. `dependencies="/depends_on"` binds
  a list of predecessor document paths, relative to the repository root (an
  optional leading slash is accepted). Finish-to-start arrows connect individual
  tasks in both views. Drag a task to move its dates or either edge to resize;
  arrow keys adjust a day (Shift: a week), and Escape cancels a drag. Changes use
  the bound frontmatter fields, stage locally, and undergo automatic validation.
  Moves preserve duration; resizes retain at least one day. Resource assignments
  and dependency links are edited in Markdown. Missing or unscheduled targets are reported below the chart.

The MIT editions of `@svar-ui/svelte-grid`, `@svar-ui/svelte-kanban`, and
`@svar-ui/svelte-gantt` provide the complex views. Their private component state is
not a second store of record. Paid scheduling features are not used. The Gantt
extension packs overlapping bars into lanes, uses uniform resource row heights,
and draws dependency arrows; SVAR owns the grid, date axis, and scrolling. A
small version-pinned pnpm patch adds an optional `compact` override so narrow
screens can retain both grid and chart rather than switching to grid-only mode.

`action(name, callback)` registers a function `(documents, event) -> edits`.
`update(doc, fields={...}, timeline=None)` produces one proposed edit. Field keys
are top-level YAML keys; timeline optionally appends a single-line entry to an
existing H2 Timeline. Kanban events contain `path`, `value` and an explicit UTC
`timestamp`. All action edits are staged together, preserving YAML comments and
body content, and pass through ordinary local validation, reconciliation and
atomic server submission. Invalid transitions remain visible drafts for review;
action execution does not bypass template validation or commit automatically.

App definition validation executes collection queries against the proposed document
inventory and checks view/action references. Action callbacks execute only when
invoked with a real event; their proposed edits go through ordinary validation.
Offline validation of an edited app requires the collection source documents.
