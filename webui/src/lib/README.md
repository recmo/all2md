# Frontend modules

- `apps/`: Starlark app evaluation, worker, and table/kanban/Gantt views.
- `documents/`: Markdown rendering, frontmatter, source editing, and syntax highlighting.
- `navigation/`: Document tree, navigation drawer, tree edits/status, and link rewriting for moves.
- `workspace/`: MCP access, inventory/cache, draft reconciliation, staged diffs, and connection settings.
- `validation/`: Local validation orchestration and its worker.
- `wasm/`: Generated bindings and binary shared by app and validation workers; rebuilt by `pnpm build:wasm`.

Keep tests beside the modules they exercise. Use relative imports within features
and explicit paths for dependencies between features. The route in
`src/routes/+page.svelte` composes these features into the workspace.

The selected document is derived from the workspace cache and selection, never
stored as a second mutable copy. Drafts, bases, conflicts, and submission text
are saved synchronously in a small localStorage journal. Published documents
and the validation baseline use a separate, asynchronous IndexedDB cache;
cache failures must not prevent saved drafts from being submitted.

App document metadata and schema ownership are projected in Rust, using the
same projection during validation and view evaluation. JavaScript supplies the
current sources, including drafts and staged deletions, without inferring
schema ownership itself.

Run `pnpm format` to format handwritten frontend files; `pnpm check` also
enforces formatting. Generated WASM bindings are excluded.
