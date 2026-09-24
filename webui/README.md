# mdstore web workspace

SvelteKit SPA with a static adapter, Pierre CodeView, markdown-it, and DOMPurify.
This sibling package owns the frontend source and tooling. Its `wasm/` crate links
the `mdstore` library with server features disabled. The Rust daemon embeds this
package's production build. There is no Node server in production.

```sh
# Requires Rust with the wasm32-unknown-unknown target and wasm-bindgen-cli 0.2.121.
pnpm install --frozen-lockfile
pnpm build
pnpm check
pnpm test
cargo build --manifest-path ../mdstore/Cargo.toml
```

For development, run a daemon and `MDSTORE_URL=http://127.0.0.1:3131 pnpm dev`.
The Vite development proxy forwards API calls to that daemon. `MDSTORE_URL` is a
server-side development setting, not a browser-selectable destination.

`pnpm test:browser` starts a fresh disposable Git repository and compiled debug
daemon for each test on port 43132. Install Playwright Chromium or set `CHROME_EXECUTABLE` to an
installed Chromium/Chrome binary. Build the frontend and Rust binary first.

## Data flow

- Search, reads, and submissions call `/mcp` (`search`, `get_page`, `apply_edits`).
- `/ui/documents` adapts public `Store::documents` for paths, permissions, and a
  repository cache namespace.
- `/ui/validate` adapts public `Store::validate_edits`, accepting the same batch
  and using the same validation path as `apply_edits`. Shared Rust validation
  runs incrementally in a WASM worker while editing; the server validates again
  before submission. `/ui/validation-snapshot` adapts public
  `Store::validation_snapshot` for validation metadata.
- Documents have Rendered and Code views. Pierre CodeView edits the complete source;
  the editor stays mounted across view switches to preserve undo and selection.
  Markdown-it and DOMPurify render prose locally; Pierre File renders fenced and
  indented code with Shiki highlighting (including Rust, LaTeX, Typst, and Lean 4).
  Starlark uses Python coloring; Markdown frontmatter uses YAML coloring.
  Configuration and template editing follow the daemon permission settings.
  The service worker caches bundled language assets for offline rendering.
- Visited documents, templates, draft bases, edits, and summaries are written to
  localStorage, scoped by origin and repository. “Make library available offline”
  caches every listed document. The service worker caches only the app shell.
- Offline search is explicitly labelled cached-text search. Reconnecting refreshes
  the listing; it never auto-submits and requires fresh validation. Exact original
  source is retained for concurrency checks, including across browser reloads.
- Connection credentials live on the Settings page; tokens remain in memory. Cache quota and concurrent-tab conflicts are surfaced;
  export drafts before closing a tab whose changes could not be saved. Local
  cached documents remain readable without a token, until the cache is cleared.

Service workers require localhost or HTTPS. An insecure non-local HTTP deployment
can keep drafts in localStorage but cannot reload the application shell offline.

The document library uses @pierre/trees through its vanilla API, hosted by a Svelte component. It provides virtualized rows, compact folder chains, keyboard navigation, and offline status badges: cached, not cached, downloading, local draft awaiting submission, or local-save failure. Folder badges aggregate offline availability. Cached does not imply up to date with the server. Folder/file context menus support creation, renaming, moving, and staged deletion.
Renames and moves update local Markdown links. MCP search retains ranked results.


Flagged app documents (`mdstore: app` in frontmatter) evaluate bounded Starlark
collections over document metadata. Table, kanban, and Gantt views use SVAR;
Gantt supports resource rows, dependencies, date dragging, and edge resizing.
Actions stage ordinary drafts. The pinned Gantt patch allows grid and chart to
remain visible together on mobile. See the daemon README for the app API.

Submit shows diffs, validation, and reconciliation against server changes. A
three-way merge automatically reconciles non-overlapping edits and presents
conflicts for explicit resolution. Nothing is submitted automatically.

The backend build reads `../webui/build` by default. Set
`MDSTORE_WEB_DIST` to an absolute build directory when packaging separately; Nix
passes the frontend derivation directly without copying it into the backend.
