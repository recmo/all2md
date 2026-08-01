# ebook2md

`ebook2md` converts papers, books, scans, and image archives into portable,
auditable Markdown bundles on Apple Silicon. Unlimited-OCR provides the visual
interpretation; embedded PDF and DjVu text is retained only as comparison
evidence and never silently replaces the visual result.

The default `thorough` quality mode begins with one ordered multi-page Base
pass. The MLX streaming decoder retains selected-token probabilities, aligns
them back to exact output substrings, and records local low-probability spans.
Only pages containing such spans, structural failures, or strong embedded-text
disagreement receive one page-local Gundam pass, always with cropping enabled
at 1024 pixels. Base remains the layout authority; an aligned Gundam block is
selected only when local probability, format integrity, and auxiliary embedded
text make it the best supported reading. `balanced` targets structural failures
and strong embedded-text disagreement without using token probability; `fast`
uses Base alone.

The model is never prompted to emit JSON or arbitrate between its own readings.
The MLX runtime keeps the vision stack and image tensors in FP32 and the
language decoder in BF16; effective precision is recorded with each invocation.

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
ebook2md convert draft.pdf --output result --quality balanced
ebook2md verify result/book
```

`--quality thorough|balanced|fast` defaults to `thorough`. Thorough conversion
spends additional inference time only where Base reports low confidence or a
structural problem, or disagrees strongly with an available embedded layer. It
runs at most one maximum-resolution Gundam reread per eligible page.

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

Each fixed-layout page record keeps `visual.multi_page`, `visual.candidates`,
`embedded`, `comparison`, canonical normalized blocks, and selection provenance
separately. Markdown is rendered only from the best canonical blocks. An
unresolved targeted reread sets block-level review metadata, increments
`metadata.json`'s `review_required_blocks`, and remains available in the JSON
artifacts without annotating the reading output; it does not withhold a best guess.
A pinned `mdformat`/GFM pass runs only when it preserves page markers, image
targets, and semantic tokens; PyMarkdown is scan-only. Conversion runs
`ebook2md verify` before reporting success. Missing optional evidence remains a
warning because OCR and extraction are intentionally best effort and can be
rerun.

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
