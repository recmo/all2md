# Decoder validation

Current evidence and limitations: [rerun readiness](rerun-readiness.md).
Agreement with embedded text is a diagnostic, not independent OCR ground truth.

Two maintained tools:

- `scripts/benchmark_decoding.py --corpus DIR --output NEW_DIR --cases PDF-STEM-PAGE`
  runs the production Base and Detail policies on selected physical pages.
  Defaults come from `scripts/hard-pages.json`; no corpus files are changed.
- `scripts/replay_decoding.py BUNDLE --pages 1 5` evaluates retained observations
  without invoking the model or changing the bundle.

Run with `PYTHONPATH=pages2md/src` and the OCR dependencies installed.
Raw outputs, source hashes, code snapshots, and diagnostics belong in the
gitignored `artifacts/` directory. Use a fresh output directory for changed code.
Inspect source renderings before accepting apparent improvements.

## Historical evidence

`archive/` preserves the exploratory reports and scripts from September 8–9.
These are unmaintained snapshots, not current usage instructions; their paths,
private backend switches, and required local artifacts refer to that experiment.
Use the originating Git revision to reproduce them. Raw evidence remains local.
