# Working preferences

## Keep the code small and direct

- Prefer less code, fewer concepts, and a smaller public surface. Optimize for
  comprehending and debugging the implementation.
- When simplifying an interface, remove obsolete implementation paths, helpers,
  dependencies, fixtures, and documentation as well. Review callers and the
  underlying engine; do not leave the old design hidden behind a new wrapper.
- Remove backwards compatibility that has no demonstrated current consumer.
  Prefer updating callers in the same PR over retaining aliases, legacy formats,
  deprecated options, or duplicate response representations.
- Avoid speculative options, abstractions, fallback behavior, and output limits.
  Check actual usage and data sizes before adding them.

## API and output design

- Return each piece of information once. Prefer source content over redundant
  derived metadata, echoed arguments, and bookkeeping fields.
- Use plain text for documents and path listings. Use structured data for results
  that need structure. Do not include a serialized JSON text copy of an MCP
  structured result for legacy clients.
- Reference schemas by path instead of expanding their definitions into reads.
  Clients can read the schema separately and derive presentation data locally.
- In public document interfaces, paths are rooted at the repository root and
  start with `/`; they are not host filesystem paths. Directories end with `/`.
  Use `/path:line` and `/path:start-end` for source locations and read windows.
- Prefer a small set of composable operations and concise names. Current MCP
  tools are `read`, `search`, and `edit`; avoid reintroducing superseded names.
- Prefer natural extensions to existing arguments: a path or path array for
  batch reads, operations grouped by path for edits, and path suffixes for tree
  traversal. Avoid parallel interfaces for the same operation.
- Keep success responses minimal. Attribute errors to an actionable `location`
  and, when available, a `schema` location. Omit unavailable fields rather than
  returning nulls or separate path/line components.
- When discussing an interface, show its actual current request schema and
  representative requests or responses. Distinguish the MCP payload from its
  JSON-RPC/HTTP envelope when explaining serialization.

## mdstore and webui

- MCP file reads always return hashline content. Raw documents and directory lists
  for the web UI use the non-MCP `GET /path/...` interface; do not restore a
  `format` option on `read`.
- Hashline anchors use four-character hashes incorporating immediate neighbors.
  Match at the supplied line position: no relocation or merging in the server
  edit engine. This does not prohibit the web UI's explicit draft reconciliation.
- Keep server edits to `replace` and `append`. Empty replacement deletes lines;
  deleting a file requires its complete anchor range. Do not restore base-text
  guards, separate create/delete operations, or empty-to-empty replacement.
- Search results contain a path/range and hashline excerpt. Avoid returning
  derived metadata, headings, scores, retrieval-arm diagnostics, or vector
  coverage as ordinary result fields.
- Preserve validation, atomicity, concurrency checks, durable retries, and crash
  recovery when removing obsolete interfaces and compatibility code.

## Development checks and committed tests

- Test PRs locally during development and before merging. Focused tests and
  reproduction scripts are temporary tools: do not commit them, or remove them
  before merging. Verification does not imply retaining a regression test.
- For mdstore/webui, keep only the small set of broad browser end-to-end tests
  covering many behaviors through a real daemon and disposable Git repository.
  Do not commit unit/API regression suites or their supporting fixtures and tools.
- The preference is to keep the maintained codebase small and debug failures
  when they arise, rather than accumulate permanent regression tests.
- The agreed test removal scope is mdstore/webui. Do not delete other packages'
  existing tests as incidental cleanup.
- Report what was actually verified and any remaining limitations. Do not claim
  that successful local checks guarantee the absence of regressions.

## Workflow and communication

- Use a worktree for changes. Continue updating the existing task branch and PR
  when working on the same change.
- Carry authorized changes through implementation, relevant local verification,
  cleanup, and the PR update. Avoid repeatedly asking for confirmation of
  decisions already made.
- Keep explanations concise and concrete. Lead with the resulting behavior;
  show examples when they clarify a contract. Explain unfamiliar fields and
  remove unnecessary concepts instead of merely renaming them.
- Keep PR descriptions aligned with the final implementation and include the
  relevant verification results.

## Nix tooling

Prefer the repository's Nix development shell for builds, tests, and tools.
Use `nix develop --command …`, selecting the package shell such as `.#mdstore`
or `.#webui` when appropriate. For repeated commands, keep an interactive
shell open with `nix develop --command bash --noprofile --norc` and reuse that
terminal session. Independent commands must enter the devshell again.

If no development shell exists, use `nix shell` for missing tools; for example,
`nix shell nixpkgs#cargo nixpkgs#rustc --command cargo build`.
Do not install persistent user tools or toolchains, including with
`nix profile install`, `cargo install`, `rustup`, or `pip install --user`.
Project-local dependencies, virtual environments, and build outputs are fine.
