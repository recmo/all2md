# all2md

Local-first tools that turn source material into auditable Markdown.

## Projects

- [`pages2md`](tools/pages2md/README.md) converts books, papers, scans, and
  image archives into portable Markdown bundles with retained OCR evidence.
- [`Meeting Capture`](apps/meeting-capture/README.md) records local microphone
  and meeting-participant audio into canonical, lossless meeting records.
- [`speech2md`](tools/speech2md/README.md) uses MOSS locally to transcribe and
  diarize recordings, with ReDimNet2 voice embeddings for stable speaker
  labels across processing windows.
- [`doc2md`](tools/doc2md/README.md) extracts Google Docs and Notion pages into
  durable Markdown with source metadata, assets, and synchronization safety.

Each project owns its runtime and dependencies. The repository shares only
stable artifact schemas and top-level build orchestration.

The Nix packages include their locked Python dependency environments in the
immutable store. Running an installed CLI never creates a virtual environment;
only model weights and checkpoints are populated in user caches on demand.

## Commands

```sh
nix run github:recmo/all2md#pages2md -- --help
nix run github:recmo/all2md#speech2md -- --help
nix run github:recmo/all2md#doc2md -- --help
nix build github:recmo/all2md#meeting-capture
nix develop .#pages2md
nix develop .#speech2md
nix develop .#doc2md
nix flake check
```

Meeting Capture is packaged as `packages.aarch64-darwin.meeting-capture` and is
developed from `apps/meeting-capture/MeetingCapture.xcodeproj`. The Nix package
uses the locally installed Xcode and installs `MeetingCapture.app` under its
`Applications` output.

`speech2md` intentionally has one processing path: pinned MOSS transcription
and diarization plus pinned ReDimNet2 speaker reconciliation, with raw model
output retained beside the rendered Markdown.
