# Recordings and transcripts

Recordings are authored documents. Transcripts and voiceprints are published by
workers and protected by mdstore. Correct the recording guidance, then submit.

```starlark
backlinks(required=False)

_range = {"type": "object", "properties": {"track": {"type": "string"}, "start": {"type": "number", "minimum": 0}, "end": {"type": "number", "minimum": 0}}, "required": ["start", "end"], "additionalProperties": False}
_attendee = {"type": "object", "properties": {"handle": {"type": "string", "minLength": 1}, "identity": {"type": "string"}, "ranges": {"type": "array", "items": _range}}, "required": ["handle", "identity"], "additionalProperties": False}
_edit = {"type": "object", "properties": {"track": {"type": "string"}, "start": {"type": "number", "minimum": 0}, "end": {"type": "number", "minimum": 0}, "before": {"type": "string", "minLength": 1}, "after": {"type": "string", "minLength": 1}}, "required": ["start", "end", "before", "after"], "additionalProperties": False}
frontmatter(
    allow_extra=True,
    mdstore=enum(["recording"]),
    audio=string(),
    manifest=string(),
    hotwords=list_of(string(min_length=1)),
    attendees=list_of(field(_attendee)),
    edits=list_of(field(_edit)),
)

def check(doc):
    if doc.frontmatter.get("mdstore") != "recording":
        return
    values = doc.frontmatter
    require(("audio" in values) != ("manifest" in values), "Specify either audio or manifest")
    for attendee in values.get("attendees", []):
        for span in attendee.get("ranges", []):
            require(span["end"] > span["start"], "Speaker range must have start < end", field="attendees")
    for edit in values.get("edits", []):
        require(edit["end"] > edit["start"], "Correction must have start < end", field="edits")

validate(check)
```
