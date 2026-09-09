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

### Markdown validation

The `markdown` checks in a directory template are independently configurable and disabled when
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
is committed. Incoming template changes are validated against every selected page before activation. Invalid batches change nothing; there is no auto-formatting.
These are explicit checks on CommonMark parsing, not a guarantee that every
typo is rejected: unmatched emphasis and undefined reference syntax can still
be ordinary text, and link fragments are not checked against headings.

### Literate directory templates

Place `template.md` in the directory whose documents it governs. It applies
recursively; the nearest ancestor template replaces its parent completely.
Template files are configuration resources: readable through `get_page`, protected
from `apply_edits`, and excluded from the document corpus and embeddings.
All templates are compiled, including those in empty or excluded directories.

Markdown headings and prose are instructions for people and agents. They impose
no implicit document rules. Only top-level fenced blocks with the exact info
string `starlark` execute. Other languages, indented code, and fences nested in
lists or blockquotes are examples. Unclosed executable fences and obsolete
`starlark schema`/`starlark validate` fence labels are rejected.

All executable blocks form one Starlark module in document order, so definitions
can be shared across blocks. The module is compiled and frozen at activation;
document validation reuses it. There are no imports, filesystem, network, clock,
or random APIs. The dialect is standard Starlark, not full Python.

For example, `tasks/v1/template.md`:

````markdown
# Tasks

Name the intended outcome and record relevant events.

```starlark
frontmatter(
    title=string(required=True, min_length=1),
    state=enum(["inbox", "ready", "waiting", "completed"], required=True),
    waiting_on=string(nullable=True),
    tags=list_of(string(), unique=True),
)
```

## Waiting

Explain the external dependency when a task is waiting.

```starlark
def check_waiting(doc):
    if doc.frontmatter["state"] == "waiting":
        require(bool((doc.frontmatter.get("waiting_on") or "").strip()),
                "Explain what this task is waiting for", field="waiting_on")

validate(check_waiting)
```

## Timeline

Record messages and decisions chronologically.

```starlark
section("Timeline", required=True, content=dated_list())
```
````

A complete [task template](examples/tasks/v1/template.md) includes filename
allocation, state transitions, metadata projection, and Timeline rules.

#### Declarations

The following helpers register inspectable schema data, which Rust validates:

- `frontmatter(**fields)`: declare top-level fields; unknown fields are rejected.
  Use `fields={...}` for names that cannot be keyword arguments, or
  `allow_extra=True` to allow additional fields. It may be called once.
- `string(required=False, nullable=False, min_length=None, max_length=None,
  pattern=None)`, `integer(required=False, nullable=False, minimum=None,
  maximum=None)`, `boolean(required=False, nullable=False)`,
  `enum(values, required=False, nullable=False)`, and
  `list_of(item, required=False, nullable=False, unique=False, min_items=0)`
  construct field definitions. Optional does not mean nullable. Values are never
  coerced and defaults are not inserted.
- `section(heading, level=2, required=False, content=None, instructions="",
  **rules)` registers an exact section heading. Siblings have the same level;
  declared sections cannot appear twice. Other sections are allowed by default.
- `dated_list(timestamp="rfc3339", order="ascending", min_items=1,
  allow_equal_timestamps=True)` is a section content rule. Entries must be
  unordered list items starting with a literal RFC3339 timestamp and explanation.
  Ordering compares instants, including timezone offsets. Equal instants are
  optionally allowed. Continuation paragraphs, links, and nested supporting
  lists are permitted; subsection headings are not.
- `filename(pattern, serial_scope=[])` constrains the entire repository-relative
  filename using a Rust regular expression. Named captures define serial scopes.
- `configure(**settings)` exposes the existing Rust policy representation for
  `frontmatter` (raw JSON Schema), `metadata`, `markdown`, `links`, `relations`,
  `structure`, `preamble`, and nested `sections`. Use this for nested object
  schemas and less common structural checks. Starlark uses `True`/`False`/`None`.

Section `content` can also be `paragraphs`, `list`, `table`, `code`,
`blockquotes`, or `empty`. Rules include `nonempty`, `include_subsections`, and
`paragraphs`, `words`, `characters`, or `list_items` bounds, each with `minimum`
and/or `maximum`. `structure` accepts `level`, `order` (`enforced` or
`unrestricted`), and `additional_sections`. Recursive section declarations are
available through `configure(sections=[...])`. Legacy `list` rules accept
`ordered`, `minimum_items`, `item_pattern`, and `date_order`; their dates are
literal YYYY-MM-DD prefixes, distinct from `dated_list` RFC3339 timestamps.

`metadata` maps output names to frontmatter JSON pointers. `links` enables
Markdown/wiki syntax. `relations` selects authored links or frontmatter arrays,
with optional reciprocal requirements. Reciprocal facts must be authored in the
same atomic edit batch; the daemon does not generate them.

#### Custom validation

`validate(callback)` registers a function taking `doc`.
`validate_change(callback)` registers a function taking `before, after`;
creation passes `before=None`, deletion passes `after=None`.
Callbacks return `None`; `require(condition, message, field=None, at=None)`
rejects the document when its condition is false. The first failing callback
stops that document's script evaluation. Schema/structure failures skip custom
document callbacks, and any document failure prevents change validation.

Inputs are immutable snapshots. Documents expose `path`, `text`, `frontmatter`
(a dictionary), `links`, `line`, and `sections` (a dictionary keyed by exact
heading text). A section exposes `text`, `level`, `line`, and `entries`.
Each top-level list entry exposes `timestamp` (RFC3339 or `None`), `text`,
`links` (destination strings), and `line`. Use `doc.frontmatter["state"]` and
`doc.sections["Timeline"].entries`. For independently addressable sections, use
unique heading names; the section lookup keeps the last same-named heading.

`field="state"` locates a top-level frontmatter field; `at=entry` or `at=section`
locates a Markdown node. Errors include template paths and Starlark source
locations; schema and section declarations also include the enclosing template
heading. YAML must be a mapping with unique keys.

Each module initialization or callback batch has a 100,000-tick budget, a 16 MiB
Starlark heap budget, and a 64-frame call-stack limit; template source is limited
to 1 MiB. These are best-effort interpreter limits, not process isolation or a
hard bound on native allocations. Templates are repository-controlled code.

Full-corpus validation remains the correctness baseline. Change rules execute
before local writes. Incoming Git commits also run parent-policy change checks
for each incoming commit edge, so a legal sequence is not collapsed into an
illegal direct transition. The final snapshot must pass its own templates.
Startup validates the current snapshot, without replaying historical transitions.
A rejected transition remains rejected in that history; correct the import or
explicitly adopt a new baseline rather than expecting a later commit to erase it.

#### Filename allocation and directory versions

Every edit uses one `path` argument. Literal paths remain literal. On creation,
`{serial}` or `{serial:03}` requests allocation; unknown placeholders, multiple
placeholders, and placeholders on non-create operations are rejected. Padding
is a minimum width (at most 12), not a limit on the serial value.

The applicable `filename` pattern must capture `serial`; `serial_scope` names
other captures, such as `year`, `month`, and `day`. Allocation chooses one more
than the greatest current serial in that scope, under the repository lock.
It includes other creates in the batch, including explicitly named creates.
No counters or ID fields are stored in the document. Deleting the highest
number can make it available again; this is not an everlasting sequence.
Accepted paths are returned in `touched_paths` and `fresh_hashlines`, and retries
use the existing durable receipts before allocation. An untracked file collision
rejects the batch. Allocation substitutes the path only, never document content.

Use `tasks/v1/template.md`, `tasks/v2/template.md`, etc. for incompatible formats.
There is no version field or registry. Old documents remain in their original
directories and editable under their original policies. Migrating a document
means explicitly moving its identity, changing content, and updating references.

`get_page` returns `template: {path, definition, content}` for documents,
including proposed in-scope paths with `exists: false`. `definition` is the
resolved declaration data; `content` is the literate template Markdown.
Templates are read-only through `apply_edits`; changes are authored externally
and activated through validated Git synchronization.

Legacy `template.yaml` remains supported during migration, with its existing
schema and structural semantics. A directory containing both `template.md` and
`template.yaml` is rejected. Replace the YAML file with Markdown in one commit.
A nearer legacy or Markdown template replaces all parent policy.

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
