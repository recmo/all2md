# speech-review

`speech-review` is a local web editor for `speech2md` review guidance. It scans a
folder recursively for generated transcripts, plays their adjacent recording
tracks, visualizes speaker turns, ranks known identities by voiceprint
similarity, and writes review decisions to the adjacent `.hint.yaml` only.

Recordings can be queued from the transcript browser without changing the
meeting open in the editor. One `speech2md` process runs at a time, and each row
shows its queue position, current model/window stage, and completed audio
percentage. The operational queue is process-local and is cleared when the
review server stops.

Transcript Markdown is treated as immutable input. Turn ends are inferred from
the next turn timestamp. For multi-track captures, the browser compares audio
activity during a turn and uses the loudest track unless an existing hint range
already identifies the track. When several turns overlap at the clicked time,
the latest-starting turn in the clicked lane wins.

A coalesced Markdown turn can be split at the audio playhead. The resulting
subranges can be assigned independently and are reconstructed from speaker hint
range boundaries when the transcript is reopened. The split does not add or
change Markdown timestamps; only assigned ranges are persisted to `.hint.yaml`.
Every persisted range remains visible as a `GUIDED` subrange, including ranges
whose identity already agrees with the currently rendered transcript.

The right column mirrors the guidance sidecar: document metadata, hotwords, and
attendees with their speaker ranges nested underneath. Selecting an unidentified
run expands its closest voiceprint matches in place. Matches are grouped into
current attendees and identities found only in other transcripts; choosing the
latter also adds that identity to the attendee list.
Every current attendee remains assignable even when no comparable voiceprint is
available for them.
Anonymous speaker handles are listed beneath the attendees with their remaining
unnamed run counts. The transcript header can jump to the next unnamed run,
while selecting a handle in the sidebar advances through that speaker's runs.

## Run

```sh
uv run --project speech-review speech-review ~/Meetings
```

Then open <http://127.0.0.1:8765>. The default folder is the current working
directory. The service binds to localhost unless `--host` is provided.

Ordinary edits never modify `.md`, audio, or `.voiceprints.npz` files. Raw
recordings and recordings with an unsupported older transcript are still
listed as `unprocessed` or `stale`. The Regenerate control is the explicit
boundary that runs current `speech2md --force` and replaces derived Markdown
and voiceprints; it always asks for confirmation first.

## Hint extensions

In addition to `hotwords` and `speakers`, the editor stores:

```yaml
title: ProveKit weekly check-in
started_at: '2026-08-04T09:00:00+02:00'
ended_at: '2026-08-04T10:00:00+02:00'
calendar_event: https://calendar.google.com/example
attendees:
  - Michał
edits:
  - track: participants
    start: 1122
    end: 1148
    before: F two Z
    after: F2Z
```

Attendees do not need speaker ranges. Localized edits are applied during
explicit regeneration only when their time range, optional track, and original
text identify exactly one occurrence. A current transcript is marked `stale`
whenever the adjacent hint hash differs from the `hints_sha256` rendered into
its frontmatter.
