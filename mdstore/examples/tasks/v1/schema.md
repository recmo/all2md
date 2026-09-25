# Tasks

A task describes one concrete intended outcome. Keep its current state in
frontmatter and its relevant messages, decisions, and state changes in Timeline.

Use the first H1 heading as the task title. Do not duplicate it in frontmatter.

## Frontmatter

Use `inbox` for captured work, `ready` for clarified work, and `waiting` for an
external dependency. Completed and cancelled tasks remain as historical records.

```starlark
frontmatter(
    state=enum(["inbox", "ready", "waiting", "completed", "cancelled"], required=True),
    waiting_on=string(nullable=True),
    tags=list_of(string(min_length=1), unique=True),
    start=string(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
    end=string(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
    assignee=string(min_length=1),
    effort=integer(minimum=0),
    depends_on=list_of(string(min_length=1), unique=True),
)

relation("depends_on", selector={"kind": "frontmatter", "array_pointer": "/depends_on", "target_pointer": ""})
metadata(state="/state", tags="/tags")
markdown("rumdl.toml")

def check_waiting(doc):
    require(bool(doc.title.strip()), "Add an H1 heading for the task title")
    if doc.frontmatter["state"] == "waiting":
        require(
            bool((doc.frontmatter.get("waiting_on") or "").strip()),
            "Name the person or event this task is waiting for",
            field="waiting_on",
        )

validate(check_waiting)
```

## Filename

The repository-relative filename is the identity. Allocate serials per day,
across all slugs. New tasks may use `{serial:03}` in their requested path.

```starlark
filename(
    r"tasks/v1/(?P<year>[0-9]{4})/(?P<month>0[1-9]|1[0-2])/(?P<day>0[1-9]|[12][0-9]|3[01])-(?P<serial>[0-9]+)-[a-z0-9]+(?:-[a-z0-9]+)*\.md",
    serial_scope=["year", "month", "day"],
)
```

## Timeline

Include exactly one H2 Timeline section, with at least one entry. Record events
in chronological order. Use a literal RFC3339 timestamp followed by an explanation.
Include Markdown source links when available. Continuation paragraphs and nested
supporting lists are allowed.

```starlark
section("Timeline", required=True, content=dated_list())
```

For example:

```markdown
## Timeline

- 2026-09-09T10:00:00Z — Captured: review the proposal.
  Source: [Message](msgvault-message:123).
- 2026-09-09T11:00:00Z — Clarified the acceptance criteria; moved to ready.
```

## State changes

Clarify inbox tasks before completing them. Add a Timeline entry when changing
state. Correcting an earlier entry is allowed; history is not mechanically
append-only. Creation and deletion have no additional transition restrictions.

```starlark
transitions = {
    "inbox": ["ready", "cancelled"],
    "ready": ["waiting", "completed", "cancelled"],
    "waiting": ["ready", "completed", "cancelled"],
    "completed": ["ready"],
    "cancelled": ["inbox", "ready"],
}

def check_transition(before, after):
    if before == None or after == None:
        return
    old = before.frontmatter["state"]
    new = after.frontmatter["state"]
    if old != new:
        require(new in transitions[old], "Transition %s -> %s is not permitted" % (old, new), field="state")
        require(
            len(after.sections["Timeline"].entries) > len(before.sections["Timeline"].entries),
            "Add a Timeline entry explaining the state change",
            at=after.sections["Timeline"],
        )

validate_change(check_transition)
```
