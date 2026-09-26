# mdstore web workspace

SvelteKit SPA with a static adapter, Pierre CodeView, markdown-it, and DOMPurify.
This package owns the frontend, offline-validation orchestration, and Starlark app
runtime. Its `wasm/` crate links mdstore's generic validation library. The document
server has no dependency on this package and does not host the UI.

Frontend modules are grouped by feature; see [the source layout](src/lib/README.md).

```sh
# Requires Rust with the wasm32-unknown-unknown target and wasm-bindgen-cli 0.2.121.
pnpm install --frozen-lockfile
pnpm build
pnpm check
pnpm test
cargo test --manifest-path wasm/Cargo.toml --lib
cargo build --manifest-path ../mdstore/Cargo.toml
# In separate terminals, run mdstore serve and:
MDSTORE_URL=http://127.0.0.1:3131 pnpm preview
```

For development, run a daemon and `MDSTORE_URL=http://127.0.0.1:3131 pnpm dev`.
Open `/webui/`. The Vite development proxy forwards paths outside `/webui/` to that daemon. `MDSTORE_URL` is a
server-side development setting, not a browser-selectable destination.

`pnpm test:browser` starts a fresh disposable Git repository and compiled debug
daemon for each test on port 43133, with a separate UI proxy on port 43132. Install Playwright Chromium or set `CHROME_EXECUTABLE` to an
installed Chromium/Chrome binary. Build the frontend and Rust binary first.

## Data flow

- Search and atomic submissions call `/mcp` (`search`, `edit`). Reads use
  repository URLs: directory JSON at `/<folder>/`, page metadata at `/<path>`
  with `Accept: application/vnd.mdstore.page+json`.
- `get` reads directories as well as files. Webui traverses `/`, reuses
  cached sources with matching hashes, and retries if revisions change during
  traversal. It builds its validation baseline locally in a WASM worker.
- Shared Rust validation runs incrementally in WASM while editing. The server
  independently validates every submission through `edit`.
- Documents have Rendered and Code views. Pierre CodeView edits the complete source;
  the editor stays mounted across view switches to preserve undo and selection.
  Markdown-it and DOMPurify render prose locally; Pierre File renders fenced and
  indented code with Shiki highlighting (including Rust, LaTeX, Typst, and Lean 4).
  Starlark uses Python coloring; Markdown frontmatter uses YAML coloring.
  Configuration and template editing follow the daemon permission settings.
  The service worker caches bundled language assets for offline rendering.
- Draft bases, edits, conflicts, and summaries use a synchronous localStorage
  journal, scoped by origin and repository. Published documents and validation
  snapshots use a separate IndexedDB cache with per-document writes. Connecting
  caches the published corpus; cache failures do not block saved drafts from
  being submitted. The service worker caches only the app shell.
- Offline search is explicitly labelled cached-text search. Reconnecting refreshes
  the listing; it never auto-submits and requires fresh validation. Exact original
  source is retained for concurrency checks, including across browser reloads.
- Connection credentials live on the Settings page; the API key is exchanged for an HttpOnly session cookie and then discarded. Cache quota and concurrent-tab conflicts are surfaced;
  export drafts before closing a tab whose changes could not be saved. Local
  cached documents remain readable after logout, until the cache is cleared.

Service workers require localhost or HTTPS. An insecure non-local HTTP deployment
can keep drafts in localStorage but cannot reload the application shell offline.

The document library uses @pierre/trees through its vanilla API, hosted by a Svelte component. It provides virtualized rows, compact folder chains, keyboard navigation, and offline status badges: cached, not cached, downloading, local draft awaiting submission, or local-save failure. Folder badges aggregate offline availability. Cached does not imply up to date with the server. Folder/file context menus support creation, renaming, moving, and staged deletion.
Renames and moves update local Markdown links. MCP search retains ranked results.


Flagged app documents (`mdstore: app` in frontmatter) evaluate bounded Starlark
collections over document metadata. Table, kanban, and Gantt views use SVAR;
Gantt supports resource rows, dependencies, date dragging, and edge resizing.
Actions stage ordinary drafts. The pinned Gantt patch allows grid and chart to
remain visible together on mobile. See the app API below.

Submit shows diffs, validation, and reconciliation against server changes. A
three-way merge automatically reconciles non-overlapping edits and presents
conflicts for explicit resolution. Nothing is submitted automatically. An uncertain submission response is retried once
with the identical request, using the server's durable batch receipt.

One shared SSE connection at `/mcp/events` refreshes the workspace on repository
changes and recording review on job progress. Reconnection receives current state;
normal GET requests fetch the data. Recording review has no polling timer.

For production, mount `build/` at `/webui/` with SPA fallback and proxy all other
paths to mdstore on the same origin. Preserve cookies, authorization, Host, and
Origin headers. `/health`, `/mcp`, `/worker`, and `/webui` are reserved names. The frontend
remains a separate artifact; a separate frontend process can provide this proxy.
Vite dev/preview provides it locally via `MDSTORE_URL`.
`nix build .#webui` produces static assets, independently of `.#mdstore`.

## Offline validation

The browser executes the same Rust validation library in a Web Worker,
including the Starlark interpreter, schema checks, Markdown rules, link resolution,
and transition checks. `pnpm build` builds the WASM module and generated bindings
before bundling the SPA; Nix builds it as a separate dependency. Generated binaries
and bindings are not committed. Run `pnpm build:wasm` before
standalone frontend type checks in a fresh checkout.

Webui builds its own versioned baseline from ordinary `get` reads: source
hashes, parsed document facts, relation edges, and configuration/template sources.
The first connection downloads the published document inventory; subsequent
refreshes reuse unchanged sources unless schema/configuration changes require
refreshing template projections. Validation uses that cached baseline offline.
When document-selection globs change, the current client marks local validation
incomplete and permits submission for authoritative checks; it never labels
those edits valid based on an incomplete inventory.

The app caches the baseline, worker and WASM binary for offline use. Local results
are advisory against the cached revision; submission always runs authoritative
server validation, permissions and optimistic concurrency checks again. Revalidate
refreshes the baseline when online. The worker preserves Starlark's execution
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

Client-side app definition validation executes collection queries against the proposed document
inventory and checks view/action references. Action callbacks execute only when
invoked with a real event; their proposed edits go through ordinary validation.
Offline validation of an edited app requires the collection source documents.

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


Schema-aware moves rewrite configured wiki targets and frontmatter relations in
addition to Markdown links. Task `depends_on` fields are declared as relations,
so both local and server validation reject dangling dependencies. Rewriting uses
the portable Rust resolver, preserves wiki labels/fragments and YAML comments,
and stages all affected documents together. A move is rejected if its new names
cannot preserve references under the governing schema.

## Recording review

Documents marked `mdstore: recording` open the integrated recording review view.
See [recording review](SPEECH_REVIEW.md) for setup, storage, worker operation,
voiceprint references, and current limitations.

## Importing files and folders

Drop Markdown or media files, or whole folders, onto the workspace. Dropping on
a tree folder selects it as the destination; dropping on a file selects its
parent. The import dialog preserves the selected folder hierarchy and lets you
change the destination before starting. The **Import** tree item also offers file and
folder pickers.

Markdown becomes local drafts using the normal validation and submission flow;
media and capture JSON files upload immediately through the asset API. Existing
paths are never overwritten. Failed transfers show per-file errors and can be
retried without repeating successful imports. Markdown can be staged offline;
asset uploads require a connection. Unsupported files are listed as skipped, Git
metadata is excluded, and empty directories are not imported.
