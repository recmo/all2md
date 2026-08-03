# doc2md

`doc2md` extracts Google Docs and Notion pages into durable Markdown files.
It can write to a local directory or clone, update, and push a Git repository.

Generated files retain source metadata in YAML frontmatter. A manifest under
`<output-root>/.doc2md/manifest.json` makes repeated syncs idempotent and tracks
renames, assets, and deletions. Large or complete deletions require an explicit
override.

## Providers

- **Notion** uses the enhanced-Markdown API, includes transcript blocks,
  preserves LaTeX and code regions, rewrites links between imported pages, and
  mirrors temporary provider-hosted assets.
- **Google Docs** recursively traverses configured Drive folders, follows
  document and folder shortcuts, and exports Docs as `text/markdown`.

Provider access is constrained outside the tool: share only the intended pages
with the Notion integration and list only the intended Drive root folder IDs.

## Install and run

```sh
uv sync --project tools/doc2md --extra dev
uv run --project tools/doc2md doc2md --help
```

Copy `doc2md.example.json`, set the environment variables it references, and
validate it:

```sh
doc2md --config doc2md.json doctor
```

Write to an existing local directory:

```sh
doc2md --config doc2md.json sync --directory ./output
doc2md --config doc2md.json status --directory ./output
```

Use `--source notion` or `--source google-docs` to run one provider. Add
`--dry-run` to fetch and render into a disposable copy without changing the
local directory or pushing Git changes.

## Configuration

Secrets may be literal strings or environment references of the form
`{"env": "VARIABLE_NAME"}`. The latter keeps credentials out of the file.

The optional `git` object supports:

```json
{
  "git": {
    "remote": "https://example.invalid/owner/documents.git",
    "branch": "main",
    "auth_header": {"env": "GIT_AUTH_HEADER"},
    "author_name": "doc2md",
    "author_email": "doc2md@localhost"
  }
}
```

When `git.remote` is configured, `--directory` may be omitted. Each run uses a
fresh clone, commits changes below the configured output root, rebases onto the
remote branch, and retries the push up to three times.

The provider objects accept optional `base_url` and `path_prefix` fields.
`path_prefix` is a list of directory names inserted below the provider name.
