# all2md

Local-first tools that turn source material into auditable Markdown.

## Projects

- [`ebook2md`](tools/ebook2md/README.md) converts books, papers, scans, and
  image archives into portable Markdown bundles with retained OCR evidence.
- [`Meeting Capture`](apps/meeting-capture/README.md) records local microphone
  and meeting-participant audio into canonical, lossless meeting records.
- `audio2md` will provide offline transcription after a representative meeting
  corpus has been collected.

Each project owns its runtime and dependencies. The repository shares only
stable artifact schemas and top-level build orchestration.

## Commands

```sh
nix run github:recmo/all2md#ebook2md -- --help
nix develop .#ebook2md
nix flake check
```

Meeting Capture is developed from
`apps/meeting-capture/MeetingCapture.xcodeproj`.

`audio2md` is intentionally absent until Meeting Capture has produced the
representative two-track corpus needed to benchmark local ASR, diarization,
alignment, and speaker-labeling models.
