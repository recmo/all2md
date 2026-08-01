# ebook2md

`ebook2md` converts papers, books, scans, and image archives into portable,
auditable Markdown bundles on Apple Silicon. Unlimited-OCR provides the visual
interpretation; embedded PDF and DjVu text is retained only as comparison
evidence and never silently replaces the visual result.

The model contract is intentionally narrow and immutable: ordered page windows
use Baidu's multi-page Base recipe, and affected pages use Baidu's Gundam recipe
for local recovery. Raw output from both observations is retained untouched;
deterministic Python code parses, validates, reconciles, structures, and renders
the result. The model is never prompted to emit JSON or arbitrate between its
own readings. The MLX runtime keeps the vision stack and image tensors in FP32
and the language decoder in BF16; effective precision is recorded with each
invocation.

## Install

The Nix app creates a locked `uv` environment in
`$XDG_CACHE_HOME/ebook2md/venv` on first use:

```sh
nix run github:recmo/ebook2md -- --help
nix run github:recmo/ebook2md -- models fetch
```

For development:

```sh
nix develop
uv sync --extra dev --extra ocr
uv run pytest
```

The OCR dependency is pinned to MLX-VLM revision
`fbdfc837da0ee197a18859ea327ede858631bdb1`; model downloads are pinned to
`baidu/Unlimited-OCR` revision
`07dea832e22aefee32ad281d4b80551282e1c168`.

## Convert

```sh
ebook2md convert paper.pdf --output result
ebook2md convert book.djvu --output result --split auto
ebook2md convert scans/ --output result --pages 1-20
ebook2md verify result/book
```

For a large document, `--split auto` creates chapter files only when reliable
structural boundaries exist. `--split chapters --chapter-map chapters.json`
accepts an explicit map:

```json
[
  {"title": "Introduction", "start_page": 1},
  {"title": "Background", "start_page": 17}
]
```

## Bundle

```text
book.md
chapters/                  # only when split
pages/page-NNNN.json       # visual and embedded evidence
raw/<observation-id>.txt   # untouched model invocation output
assets/
  figures/
  originals/
  evidence/
  manifest.json
document.json
metadata.json
```

All Markdown links are relative. Original embedded figures are preserved when
possible; rendered PNG crops are used for page-composed graphics. Asset
provenance and every placement remain available in the JSON artifacts.
Byte-identical display files are deduplicated independently from placement
records, so a reused PDF object still records every page and box.

Each fixed-layout page record keeps `visual.multi_page`, `visual.gundam`,
`embedded`, `comparison`, canonical normalized blocks, and recovery provenance
separately. Markdown is rendered only from canonical blocks. A pinned
`mdformat`/GFM pass runs only when it preserves page markers, image targets, and
semantic tokens; PyMarkdown is scan-only. Conversion runs `ebook2md verify`
before reporting success. Missing optional evidence remains a warning because
OCR and extraction are intentionally best effort and can be rerun.

## Supported inputs

- PDF
- DjVu (requires DjVuLibre; structured text, annotations, and outlines are kept
  when exposed by the file)
- EPUB, including visual OCR for pre-paginated fixed-layout books
- CBZ
- PNG, JPEG, WebP, and single- or multi-page TIFF
- naturally sorted image directories

DRM-protected books, MOBI/AZW, CBR, automatic embedded-text fusion, and
handwriting-specialized recognition are intentionally outside version 0.2.

## OCR validation

Normal tests use an injected fixture backend and do not download model weights.
Before changing the MLX-VLM or model revision, compare the result against the
reference PyTorch implementation on the representative math/layout corpus and
record latency, peak unified memory, missing figures, formula structure, and
repetition failures.
