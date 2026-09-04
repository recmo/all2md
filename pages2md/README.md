# pages2md

`pages2md` is the document-conversion tool in the
[`all2md`](https://github.com/recmo/all2md) monorepo.

`pages2md` uses Unlimited-OCR to convert paginated image sources into portable
Markdown on Apple Silicon. PDFs and DjVu files are rendered to page images just
like scans and image archives. Embedded text, outlines, metadata, links, and
PDF image objects are optional hints and enrichments: they can improve the
result, but conversion never depends on them and they never replace OCR's
reading of the page.

OCR owns the page-content inventory. In particular, an embedded PDF image
object is used only when it geometrically matches a figure detected by OCR;
otherwise the object does not create a figure in the output. When no matching
object is available, the OCR-detected figure is cropped from the rendered page.

The model contract is intentionally narrow and immutable: ordered page windows
use Baidu's multi-page Base recipe, and affected pages use Baidu's Gundam recipe
for local recovery. Deterministic Python code parses, validates, reconciles,
structures, and renders the result. The model is never prompted to emit JSON
or arbitrate between its own readings. Page results and model observations are
checkpointed in a private resumable workspace beside the input document.

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
uv sync --project pages2md --extra dev --extra ocr
uv run --project pages2md pytest
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

The input is one supported paginated image source or a directory containing
images. OCR, layout, chapter detection, and quality settings are fixed. Output is written
beside the input after replacing a file extension with `.md`: `paper.pdf`
becomes `paper.md`, while the image directory `scans/` becomes `scans.md`.
Existing output requires `--force`.

Per-page checkpoints and raw observations are retained directly in
`paper.pages2md/` beside the source.

## Output

```text
# paper.pdf, one Markdown file and no figures
paper.md

# paper.pdf, one Markdown file with figures
paper.md/
  paper.md
  figures/

# paper.pdf, multiple chapters
paper.md/
  index.md
  001-introduction.md
  002-background.md
  figures/                 # only when figures are referenced
```

No parsing records, raw model output, manifests, logs, or other intermediates
are included in the published Markdown output. They remain available in the
private `<input-name>.pages2md/` workspace beside the source so an interrupted
run can resume. Every Markdown file starts with YAML front matter containing the
SHA-256 hash of the input document and the pages2md source commit. All links are
relative, and only figures referenced by Markdown are retained.

## Supported inputs

- PDF
- DjVu (requires DjVuLibre)
- CBZ
- PNG, JPEG, WebP, and single- or multi-page TIFF
- naturally sorted image directories

EPUB, MOBI/AZW, CBR, and handwriting-specialized recognition are intentionally
outside version 0.2.

## OCR validation

Normal tests use an injected fixture backend and do not download model weights.
Before changing the MLX-VLM or model revision, compare the result against the
reference PyTorch implementation on the representative math/layout corpus and
record latency, peak unified memory, missing figures, formula structure, and
repetition failures.
