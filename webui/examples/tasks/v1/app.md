---
mdstore: app
---

# Task planner

Views of task documents, including local drafts. Scheduling and assignment fields
are optional: unscheduled tasks remain visible in the timeline view.

```starlark
def tasks(documents):
    return sorted(
        [d for d in documents if d.template == "/tasks/v1/schema.md"],
        key=lambda d: d.title,
    )

collection("tasks", tasks)

def change_state(documents, event):
    doc = [d for d in documents if d.path == event["path"]][0]
    return [update(
        doc,
        fields={"state": event["value"]},
        timeline=event["timestamp"] + " — Changed state to " + event["value"] + ".",
    )]

action("change-state", change_state)
table("Tasks", "tasks", columns=["title", "/state", "/tags", "/assignee"])
kanban("Board", "tasks", group="/state",
       columns=["inbox", "ready", "waiting", "completed", "cancelled"],
       on_move="change-state")
gantt("Schedule", "tasks", start="/start", end="/end", dependencies="/depends_on")
gantt("Workload", "tasks", start="/start", end="/end", group="/assignee", dependencies="/depends_on")
```
