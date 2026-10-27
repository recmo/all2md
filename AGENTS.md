# Working preferences

- Prefer small, direct code that is easy to understand and debug. Keep APIs and
  output minimal; avoid redundant information and speculative abstractions.
- Remove obsolete code, backwards compatibility, options, and dependencies when
  simplifying a design. Update callers and documentation together.
- Test changes locally before merging. Keep focused tests temporary and remove
  them before merging; retain only a few broad end-to-end tests when useful.
- Use a worktree. Carry authorized work through implementation, verification,
  cleanup, and the existing PR update without repeatedly asking for confirmation.
- Communicate briefly and concretely. Lead with the outcome, use examples when
  helpful, and report what was verified and any remaining limitations.
- Prefer the repository's Nix development shell for builds, checks, and tools:
  `nix develop --command …`. Reuse an interactive devshell for repeated commands;
  independent commands must enter it again. If none exists, use `nix shell` for
  missing tools. Do not install persistent user tools or toolchains; project-local
  dependencies and build outputs are fine.
