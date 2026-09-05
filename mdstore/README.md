# mdstore

`mdstore` is a Rust daemon for a Git-tracked Markdown knowledge repository. It
validates repository-defined schemas, exposes hashline-safe atomic edits, keeps
adjacent binary embedding sidecars, and serves exact, vector, graph-assisted,
and reranked search over MCP.

Markdown and `.mdstore/` configuration are canonical. `*.mdstore` embedding
files are disposable and must be ignored by Git. Because the configuration
directory has the same suffix, repositories should use both rules:

```gitignore
*.mdstore
!.mdstore/
```

## Repository configuration

Every served repository must track `.mdstore/config.yaml`. Document fields,
section names, relation types, and reciprocity are configuration rather than
Rust conventions. A minimal example is:

```yaml
documents:
  include: ["**/*.md"]
  exclude: ["archive/**"]

schemas:
  - include: "people/**/*.md"
    schema: ".mdstore/schemas/person.json"

markdown:
  closed_fences: true
  fence_language: true
  nonempty_headings: true
  heading_increment: true
  nonempty_links: true
  no_trailing_whitespace: true
  no_tabs: true
  max_line_length: 120
  final_newline: true
metadata:
  display_name: /name
  tags: /tags

links:
  markdown: true
  wiki:
    - '\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]'

relations:
  - name: mentions
    reciprocal: mentioned_by
    selector:
      kind: markdown_links
      include: "people/**/*.md"
      section: Mentions
      syntax: markdown
  - name: mentioned_by
    reciprocal: mentions
    selector:
      kind: frontmatter
      array_pointer: /backlinks
      target_pointer: /target

chunking:
  target_tokens: 400
  overlap_percent: 15
  max_chars: 2000
  exclude_sections: []
  context_pointers: [/name]

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
  batch_size: 64
  request_timeout_seconds: 30

git:
  push: true
  push_timeout_seconds: 30

server:
  listen: 127.0.0.1:3131
  bearer_token_env: MDSTORE_BEARER_TOKEN
```

The example `mentioned_by` representation is deliberately configured; mdstore
does not create it. A batch adding `mentions` must also contain the matching
`mentioned_by` edit or whole-tree validation rejects the batch.

### Markdown validation

The `markdown` checks above are independently configurable and disabled when
omitted. They impose no required frontmatter, section names, or initial heading
level. `heading_increment` rejects jumps such as H2 to H4; decreasing levels is
allowed. `fence_language` requires a nonempty info string, not a fixed language
list. `nonempty_links` checks parsed Markdown link destinations.

Whitespace and line-length checks cover the body, excluding fenced and indented
code blocks. Exactly two trailing spaces on a nonblank line are allowed for
Markdown hard breaks. Line length counts Unicode characters, not bytes; CRLF
and LF endings are accepted. `final_newline` covers the entire nonempty file.

Failures include the rule name, page path, and one-based source line. Checks run
at startup, on `validate`, and against the complete proposed tree before an edit
is committed. Enabling checks through a configuration-only edit first validates
every selected page. Invalid batches change nothing; there is no auto-formatting.
These are explicit checks on CommonMark parsing, not a guarantee that every
typo is rejected: unmatched emphasis and undefined reference syntax can still
be ordinary text, and link fragments are not checked against headings.

### Directory templates

Place `template.yaml` in the directory whose documents it governs. It applies
recursively; the nearest ancestor template replaces its parent completely,
without merging. Templates are ordinary versioned YAML files, not sidecars.
All template definitions are checked, including those in empty directories.
Existing repository-wide schema, style, and section constraints still apply.

Example `people/template.yaml`:

```yaml
instructions: Describe established facts, not speculation.
examples: []
structure:
  level: 2
  order: enforced
  additional_sections: false
preamble:
  words: {maximum: 10}
sections:
  - heading: Summary
    instructions: Summarize the person in one concise paragraph.
    rules:
      required: true
      content: paragraphs
      paragraphs: {minimum: 1, maximum: 1}
      words: {maximum: 100}
  - heading: Timeline
    instructions: Record meaningful interactions, newest first.
    rules:
      required: true
      list:
        ordered: false
        minimum_items: 1
        date_order: descending
```

Each section can have its own `instructions`, `examples`, `rules`, `structure`,
and nested `sections`. Heading names are exact, siblings must be unique, and
duplicate authored sections are rejected. `rules.required` defaults to false.
`structure.level` defaults to one level below the parent (H1 at the root),
`order` defaults to `unrestricted`, and `additional_sections` defaults to false.
Initial headings above the root section level, such as an H1 document title
before H2 sections, belong to the preamble. Structural headings must be outside
lists and blockquotes. Extra sections, when allowed, have unconstrained content.

`preamble` uses the same content rules as sections. `content` may be
`paragraphs`, `list`, `table`, `code`, `blockquotes`, or `empty`; omission allows
any block type. `nonempty: true` requires non-whitespace text. `paragraphs`,
`words`, `characters`, and `list_items` each accept optional `minimum` and
`maximum`. Paragraphs are AST paragraphs, list-item counts include nested items,
words are whitespace-delimited, and characters are Unicode scalar values of
text with Markdown delimiters removed (not browser layout or grapheme counts).
Block boundaries separate words; inline emphasis does not. Raw HTML is not
rendered. Limits cover a section's own content unless
`include_subsections: true`. `rules.list` uses the dated-list and item-pattern
rules documented below. Instructions and examples are advisory, never evaluated
by an LLM or used as blocking checks.

`get_page` includes `exists` and `template: {path, definition}` for Markdown.
For a proposed, in-scope Markdown path it returns `exists: false`, empty content,
and the applicable template, so agents can discover requirements before writing.
Read the template itself through `get_page("people/template.yaml")` for hashline
text. The same information is available through `mdstore get`.

Templates are read-only through `apply_edits`, including creation and removal.
Template editing/approval is not implemented. An externally committed template
change is activated only if full-corpus validation succeeds. Direct untracked
Markdown and template additions are quarantined by repository recovery rather
than becoming an alternate source of authored state; sidecars remain disposable
and Gitignored. Invalid edits reject the complete batch and include the template
path, source line, and relevant section guidance in findings.

### Folder-specific section structure

Section rules are additive: every matching `include` glob applies. Omit `include`
to apply a rule corpus-wide. Nothing requires a section named Timeline unless
the repository configures it, for example:

```yaml
sections:
  - include: 'people/**'
    heading: Timeline
    required: true
    maximum: 1
    level: 2
    list:
      ordered: false
      minimum_items: 1
      date_order: descending
  - include: 'projects/**'
    heading: Milestones
    required: true
    list:
      ordered: true
      minimum_items: 1
      item_pattern: '^M[0-9]+: '
```

The first rule accepts:

```markdown
## Timeline

- 2026-09-05 Released the first version.
- 2026-09-01 Started implementation.
```

`list` requires an uncontained heading and a body consisting only of lists,
ending at the next uncontained heading of equal or lower depth. Prose and
subsections are rejected; continuation paragraphs and nested supporting lists
inside an item are allowed. Item constraints apply only to top-level items.
`ordered: true` requires numbered lists; `false` requires bullets; omission
allows either. `minimum_items` defaults to zero.

`date_order` accepts `ascending` or `descending`, permits equal dates, and
requires each item to begin with a literal, valid Gregorian `YYYY-MM-DD` date
(years 0001–9999), followed by whitespace or the end of the item. Bold/code
wrapping, timestamps, and non-zero-padded dates do not satisfy this rule.
Ordering continues across all lists in the section. `item_pattern` optionally
matches raw Markdown after the list marker; anchor it with `^` to require a
prefix. Omitting `list` retains the existing heading-count checks, with optional
`level` enforcement. These rules use the same whole-corpus, atomic validation
path as schema and style checks.

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
mdstore --root /path/to/brain get .mdstore/config.yaml
mdstore --root /path/to/brain apply --file edits.json
mdstore --root /path/to/brain reindex
mdstore --root /path/to/brain status
mdstore --root /path/to/brain push
```

An edit request uses `LINE:HASH` anchors returned by `get_page`:

```json
{
  "edit_summary": "Link Alice and Bob",
  "edits": [
    {
      "op": "insert_after",
      "path": "people/alice.md",
      "anchor": "12:a3",
      "content": "- [Bob](bob.md)"
    },
    {
      "op": "insert_after",
      "path": "people/bob.md",
      "anchor": "9:f1",
      "content": "- [Alice](alice.md)"
    }
  ]
}
```

All anchors in a request resolve against the same pre-edit snapshot. Stale,
ambiguous, or overlapping edits fail before any worktree file changes.

The MCP endpoint is `/mcp`; health and indexing coverage are available from
`/health`. A configured bearer token protects both endpoints, and listening
beyond loopback is refused without one. Startup reuses valid sidecars and
rebuilds only missing or stale vectors; the explicit `reindex` command forces a
complete rebuild.
