# mdstore web workspace

SvelteKit SPA with a static adapter, Pierre CodeView, markdown-it, and DOMPurify.
This package owns the frontend, local validation orchestration, and Starlark app
runtime. Its `wasm/` crate links mdstore's generic validation library. The document
server has no dependency on this package and does not host the UI.

Frontend modules are grouped by feature; see [the source layout](src/lib/README.md).

From the repository root, run `nix develop .#webui`, then `cd webui`.
The shell provides the Rust, Node, WASM linker and CLI tools used below.
Nix selects the wasm-bindgen CLI version from `wasm/Cargo.lock`.

```sh
# Outside Nix, install Rust with the wasm32-unknown-unknown target and a
# wasm-bindgen-cli version matching wasm-bindgen in wasm/Cargo.lock.
pnpm install --frozen-lockfile
pnpm build
pnpm check
cargo build --manifest-path ../mdstore/Cargo.toml
# In separate terminals, run mdstore serve and:
MDSTORE_URL=http://127.0.0.1:3131 pnpm preview
```

For development, run a daemon and `MDSTORE_URL=http://127.0.0.1:3131 pnpm dev`.
The Vite development proxy forwards API calls to that daemon. `MDSTORE_URL` is a
server-side development setting, not a browser-selectable destination.

`pnpm test:browser` starts a fresh disposable Git repository and compiled debug
daemon for each test on port 43133, with a separate UI proxy on port 43132. Install Playwright Chromium or set `CHROME_EXECUTABLE` to an
installed Chromium/Chrome binary. Build the frontend and Rust binary first.

The only committed tests are three broad browser end-to-end workflows. Run these
and the frontend checks before merging. Test individual changes locally with
temporary checks or reproduction scripts; do not commit those tests, or remove
them before merging.

## Data flow

- Search and submissions call `/mcp` (`search`, `edit`).
- Raw document reads use `GET /path/document.md`; `GET /path/directory/` lists
  child paths. Webui traverses the tree and builds its validation baseline
  locally in a WASM worker. Schema paths arrive in the HTTP `Link` header.
  GET `X-Mdstore-Commit` values identify the published commit: cached files are
  reused only at that commit, and traversal retries if revisions differ. HTTP caching
  revalidates reads using `ETag` and permits 304 responses without downloading
  the body. The commit header is independent of compression or proxy changes to
  the HTTP cache validator.
- Shared Rust validation runs incrementally in WASM while editing. The server
  independently validates every submission through `edit`.
- Documents have Rendered and Code views. Pierre CodeView edits the complete source;
  the editor stays mounted across view switches to preserve undo and selection.
  Markdown-it and DOMPurify render prose locally; Pierre File renders fenced and
  indented code with Shiki highlighting (including Rust, LaTeX, Typst, and Lean 4).
  Starlark uses Python coloring; Markdown frontmatter uses YAML coloring.
  Configuration and template editing follow the daemon permission settings.
- Draft bases, edits, conflicts, and summaries use a synchronous localStorage
  journal, scoped by origin and repository. Published documents and validation
  snapshots stay in memory. Connecting loads the published corpus and reconciles
  drafts against current server sources; nothing is submitted automatically.
- Search always uses the daemon. Browsing, source loading, validation and submission
  require a connection. Local drafts survive refresh and temporary disconnections.
- Settings provides connection retry and discarding unsubmitted changes. Storage
  quota and concurrent-tab conflicts are surfaced so unsaved work can be recovered.

The WebUI uses the browser's HTTP cache for static assets. It does not register a
service worker, persist published documents, or provide an offline application shell.

If Connect reports a missing repository revision (`X-Mdstore-Commit`), check that
the reverse proxy forwards `/path` and `/path/*` to mdstore instead of the static
app shell, and deploy matching daemon and WebUI versions.

The document library uses @pierre/trees through its vanilla API, hosted by a Svelte
component. Folder/file context menus support creation, renaming, moving, and staged
deletion. Badges show drafts, staged deletions, validation findings, save failures,
and merge conflicts; folder badges aggregate staged changes.
Markdown and wiki links use source-relative paths; a leading `/` selects the
repository root. There is no global name search or implicit root fallback.
Missing targets are marked as broken, including against local drafts and staged
deletions. Renames and moves preserve relative/rooted link styles and rebase
outgoing links when their source document moves. MCP search retains ranked results.


Flagged app documents (`mdstore: app` in frontmatter) evaluate bounded Starlark
collections over document metadata. Table, kanban, and Gantt views use SVAR;
Gantt supports resource rows, dependencies, date dragging, and edge resizing.
Actions stage ordinary drafts. The pinned Gantt patch allows grid and chart to
remain visible together on mobile. See the app API below.

Submit shows diffs, validation, and reconciliation against server changes. A
three-way merge automatically reconciles non-overlapping edits and presents
conflicts for explicit resolution. Nothing is submitted automatically.

For production, host `build/` with a static web server and reverse-proxy `/mcp`, `/path` and
`/health` to mdstore on the same origin. Restrict access at the reverse proxy. Vite dev/preview provides that
proxy locally via `MDSTORE_URL`; preview is for local inspection.
`nix build .#webui` produces static assets, independently of `.#mdstore`.

## Local validation

The browser executes the same Rust validation library in a Web Worker,
including the Starlark interpreter, schema checks, Markdown rules, link resolution,
and transition checks. `pnpm build` builds the WASM module and generated bindings
before bundling the SPA; Nix builds it as a separate dependency. Generated binaries
and bindings are not committed. Run `pnpm build:wasm` before
standalone frontend type checks in a fresh checkout.

Webui builds its own versioned baseline from raw HTTP reads: source
hashes, parsed document facts, relation edges, and configuration/template sources.
The first connection downloads the published document inventory; subsequent
refreshes reuse files at the same commit and fetch fresh sources when the commit
changes, including updated template projections. Validation uses this in-memory baseline while connected.
When document-selection globs change, the current client marks local validation
incomplete and permits submission for authoritative checks; it never labels
those edits valid based on an incomplete inventory.

Local results are advisory against the loaded revision; submission always runs authoritative
server validation, permissions and optimistic concurrency checks again. Revalidate
refreshes the baseline from the daemon. The worker preserves Starlark's execution
limits and is terminated if a request exceeds 15 seconds.


## Starlark apps and collections

To keep an app beside a task schema, add `scope(exclude=["app.md"])` to that
schema. This is a generic path exclusion: the app inherits the parent document
schema. The `mdstore: app` flag only selects the web UI renderer and client app
validation; it has no special meaning or permissions in mdstore.

A Markdown file with `mdstore: app` in YAML frontmatter defines an app using
top-level Starlark fences. Open the file to render its views; Edit shows its source.
App files can sit beside their schema and are excluded from collection records
by the client. Schema selection follows explicit template scope rules. The client
validates their Starlark declarations; the server validates them as documents. See
[the task planner](examples/tasks/v1/app.md) for a complete example.

`collection(name, query)` registers a plain function over an in-memory list of
immutable document records. Records expose `path`, `title` (first H1, filename
fallback), `template` (root-relative path with leading slash), `frontmatter`, and
`text` (nullable for metadata-only records). Template and app definition files are
not collection records. Queries can use comprehensions, sorting and dictionaries;
there is no query DSL, database or index. Local drafts replace baseline records;
staged deletions disappear. Missing metadata and connection status are displayed explicitly. Evaluation runs in a bounded WASM worker without filesystem, network,
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

Client-side app definition validation executes collection queries against the proposed document
inventory and checks view/action references. Action callbacks execute only when
invoked with a real event; their proposed edits go through ordinary validation.
Local validation of an edited app requires the collection source documents.

### Reconciling browser drafts

Opening a staged document, visiting Submit, reconnecting, or checking server
changes fetches current source and performs a client-side three-way merge against
the draft's original base. Disjoint edits merge automatically. Conflicts preserve
both versions and the original base in local storage; the tree marks them and
Submit offers passage-by-passage Yours, Server, and manual resolutions using
Pierre diffs. Manual resolutions are saved locally.
Resolving updates the exact base and triggers validation again.

Delete/modify, remote deletion, and colliding new files require explicit choices.
Moves remain staged as source deletion and destination creation; a remotely
modified source is therefore a deletion conflict, and keeping the source leaves
the destination staged as a copy. No remote rename is inferred from filenames.
Submission checks current sources again, and the server still validates and
compares bases atomically. Changes that arrive during submission are reconciled
for another review instead of silently overwriting either version.


Schema-aware moves rewrite configured wiki targets and frontmatter relations in
addition to Markdown links. Task `depends_on` fields are declared as relations,
so both local and server validation reject dangling dependencies. Rewriting uses
the portable Rust resolver, preserves wiki labels/fragments and YAML comments,
and stages all affected documents together. A move is rejected if its new names
cannot preserve references under the governing schema.
