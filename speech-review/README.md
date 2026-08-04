# speech-review

`speech-review` is a local web editor for `speech2md` review hints. It scans a
folder recursively for generated transcripts, plays their adjacent recording
tracks, visualizes speaker turns, ranks known identities by voiceprint
similarity, and writes review decisions to the adjacent `.hint.yaml` only.

Transcript Markdown is treated as immutable input. Turn ends are inferred from
the next turn timestamp. For multi-track captures, the browser compares audio
activity during a turn and uses the loudest track unless an existing hint range
already identifies the track. When several turns overlap at the clicked time,
the latest-starting turn in the clicked lane wins.

## Run

```sh
uv run --project speech-review speech-review ~/Meetings
```

Then open <http://127.0.0.1:8765>. The default folder is the current working
directory. The service binds to localhost unless `--host` is provided.

Ordinary edits never modify `.md`, audio, or `.voiceprints.npz` files. The
Regenerate control is deliberately only a reminder of the explicit
`speech2md --force` boundary for now.

## Hint extensions

In addition to `hotwords` and `speakers`, the editor stores:

```yaml
attendees:
  - identity: Michał
edits:
  - track: participants
    start: 1122
    end: 1148
    before: F two Z
    after: F2Z
```

Attendees do not need speaker ranges. Localized edits are applied during
explicit regeneration only when their time range, optional track, and original
text identify exactly one occurrence.
