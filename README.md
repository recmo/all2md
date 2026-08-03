# all2md

Local-first tools that turn source material into auditable Markdown.

## Projects

- [`ebook2md`](tools/ebook2md/README.md) converts books, papers, scans, and
  image archives into portable Markdown bundles with retained OCR evidence.
- [`Meeting Capture`](apps/meeting-capture/README.md) records local microphone
  and meeting-participant audio into canonical, lossless meeting records.
- [`audio2md`](tools/audio2md/README.md) uses MOSS locally to transcribe and
  diarize recordings, with ReDimNet2 voice embeddings for stable speaker
  labels across processing windows.

Each project owns its runtime and dependencies. The repository shares only
stable artifact schemas and top-level build orchestration.

## Commands

```sh
nix run github:recmo/all2md#ebook2md -- --help
nix run github:recmo/all2md#audio2md -- --help
nix develop .#ebook2md
nix develop .#audio2md
nix flake check
```

Meeting Capture is developed from
`apps/meeting-capture/MeetingCapture.xcodeproj`.

`audio2md` intentionally has one processing path: pinned MOSS transcription
and diarization plus pinned ReDimNet2 speaker reconciliation, with raw model
output retained beside the rendered Markdown.
