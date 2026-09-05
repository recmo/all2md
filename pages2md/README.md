# pages2md

`pages2md` is the document-conversion tool in the
[`all2md`](https://github.com/recmo/all2md) monorepo.

`pages2md` uses Unlimited-OCR to convert paginated image sources into portable
Markdown on Apple Silicon. PDFs and DjVu files are rendered to page images just
like scans and image archives. Embedded text, outlines, metadata, links, and
PDF image objects are optional hints and enrichments: they can improve the
result, but conversion never depends on them. OCR remains authoritative for
the page-content inventory, reading order, and mathematical layout. When a PDF
also exposes character boxes and font metadata, deterministic reconciliation
may use geometrically matched glyphs to repair numeric literals, recover proof
marks, reject text-glyph crops misclassified as figures, and infer conventional
numbered-heading depth. External links are applied only to the OCR block that
contains their annotation geometry. PDF GoTo annotations remain evidence only:
page-level destinations are not precise enough to emit safe block-level links.

Text-layer quality checks require valid boxes for visible glyphs, but not for
non-ink Unicode variation selectors or whitespace. Ceiling/floor repairs match
both delimiter endpoints and enclosed content to their PDF occurrences; a
nearby ceiling cannot change unrelated probability brackets. Ambiguous matches
are left unchanged, and repeating a delimiter repair does not change its result.

Markdown linting treats `\(...\)`, `\[...\]`, `$...$`, and `$$...$$` as
opaque math regions, retaining the original line/column positions for prose
diagnostics. Code blocks, inline code, escaped delimiters, and comments are not
interpreted as formulas. Formatting preserves the exact formula source.

KaTeX separately validates each formula without changing it. The bundle's
`metadata.json` records `math_validation`: validator version/status, number of
expressions checked, and source-located diagnostics classified as `syntax`,
`unsupported`, or `resource_limit`. Unsupported commands may be valid LaTeX
outside KaTeX's supported subset. Findings are warnings, not inline review
markers or automatic repairs, and do not block publication. Successful parsing
does not certify mathematical correctness or transcription fidelity.

The Nix app and development shell include Node.js and KaTeX. For non-Nix
development, install Node.js and run `npm ci --prefix pages2md` in the repository
before using the Python CLI. No runtime downloads are performed. If validation
cannot run, the bundle explicitly records `unavailable` rather than claiming a
clean check. `PAGES2MD_NODE` and `PAGES2MD_KATEX_MODULE` can specify an existing
Node executable and KaTeX module path.

List reconstruction can retain indented paragraphs and display equations inside
an item when a following sibling marker and the page geometry corroborate that
structure. Equations remain separate typed blocks; source numbers are never
changed to silence lint. Headings, outdents, and ambiguous continuations remain
boundaries. Bare HTTP(S) URLs in prose are serialized as explicit `<…>` autolinks
without changing the address, while existing links, code, math, and reference
definitions are preserved.

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
pages2md --ignore-embedded-text poor-text-layer.pdf
```

The input is one supported paginated image source or a directory containing
images. OCR, layout, chapter detection, and quality settings are fixed. Output is written
beside the input by appending `.md` to its complete basename: `paper.pdf`
becomes `paper.pdf.md`, while the image directory `scans/` becomes `scans.md`.
Existing output requires `--force`.

Use `--ignore-embedded-text` for scans with a missing, stale, or low-quality
text layer. This disables embedded text blocks, character/font repairs, text
comparisons, and annotation-derived links. Rendering, OCR, PDF metadata and
outlines, and geometry-matched embedded image objects remain enabled. The
setting is part of the assembly fingerprint. When a workspace was created with
the other mode, pages are reassembled from its saved raw OCR observations
without rerunning the model.

Hybrid reconciliation aligns full OCR context with individual PDF glyphs,
retaining font references, baselines, and nested script relationships. It
compares geometric reading order with PDF drawing order and abstains when
glyph occurrence conflicts cannot be resolved. Script recovery additionally
requires a matching geometric parent-child structure. Math alphabet recovery uses
Unicode or recognized font encodings, including symbols OCR left outside math
delimiters; unknown encodings are left unchanged. Simple script subtrees can
be recovered from aligned glyph geometry. This is not a general PDF-to-TeX
parser: ambiguous or unsupported math remains with the visual OCR result.
Standalone proof marks are anchored to matching source text, rather than
ordered solely against approximate OCR boxes. Repairs remain in diagnostic
metadata, without adding review markers to published Markdown.

Accent reconciliation attaches unambiguous Unicode accents or known font-encoded
marks to their base glyphs and corrects conflicting single-letter LaTeX accents
(arrows, hats, tildes, bars, and dots). It does not guess unknown encodings or
wide/stacked attachments. Inline mathematical letters and simple expressions
can gain math delimiters when every letter has matching mathematical Unicode or
math-font evidence; ordinary italic prose, links, code, and ambiguous scripts
remain unchanged. This works inside structured lists as well as paragraphs.

Footnotes are matched to references using OCR footnote labels, or smaller native
body text near the bottom of a page together with a raised reference beside prose.
Recognized notes become Markdown footnotes with page-scoped unique identifiers;
each definition is placed immediately after the paragraph containing its first
reference, including in split chapters. OCR-labelled notes can
still be linked without embedded text. Unmatched or ambiguous notes remain in
place; cross-page note-body continuations are not inferred automatically.

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
