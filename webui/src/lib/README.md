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
