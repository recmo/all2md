# pages2md

`pages2md` is the document-conversion tool in the
[`all2md`](https://github.com/recmo/all2md) monorepo.

`pages2md` converts papers, books, scans, and image archives into portable
Markdown on Apple Silicon. Unlimited-OCR provides the visual
interpretation; embedded PDF and DjVu text is retained only as comparison
evidence and never silently replaces the visual result.

The model contract is intentionally narrow and immutable: ordered page windows
use Baidu's multi-page Base recipe, and affected pages use Baidu's Gundam recipe
for local recovery. Deterministic Python code parses, validates, reconciles,
structures, and renders the result in a private temporary workspace. The model
is never prompted to emit JSON or arbitrate between its own readings.

## Install

The Nix app includes its locked Python dependency environment in the immutable
Nix store. It does not create a virtual environment or install packages at
runtime. Model weights remain explicit, on-demand downloads:

```sh
nix run github:recmo/all2md#pages2md -- --help
```

For development:

```sh
nix develop .#pages2md
uv sync --project tools/pages2md --extra dev --extra ocr
uv run --project tools/pages2md pytest
```

The OCR dependency is pinned to MLX-VLM revision
`fbdfc837da0ee197a18859ea327ede858631bdb1`; model downloads are pinned to
`baidu/Unlimited-OCR` revision
`07dea832e22aefee32ad281d4b80551282e1c168`.

## Convert

```sh
pages2md paper.pdf
pages2md scans/
pages2md --force paper.pdf
```

The input is one supported document or a directory containing images. OCR,
layout, chapter detection, and quality settings are fixed. Output is written
beside the input by appending `.md` to its complete basename: `paper.pdf`
becomes `paper.pdf.md`, while the image directory `scans/` becomes `scans.md`.
Existing output requires `--force`.

## Output

```text
# paper.pdf, one Markdown file and no figures
paper.pdf.md

# paper.pdf, one Markdown file with figures
paper.pdf.md/
  paper.pdf.md
  figures/

# paper.pdf, multiple chapters
paper.pdf.md/
  index.md
  001-introduction.md
  002-background.md
  figures/                 # only when figures are referenced
```

No parsing records, raw model output, manifests, logs, or other intermediates
are published. Every Markdown file starts with YAML front matter containing the
SHA-256 hash of the input document and the pages2md source commit. All links are
relative, and only figures referenced by Markdown are retained.

## Supported inputs

- PDF
- DjVu (requires DjVuLibre)
- EPUB
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
