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

The visual model contract is intentionally narrow: ordered page windows
use Baidu's multi-page Base recipe, and affected pages use Baidu's Gundam recipe
for local recovery (`base_size=1024`, `image_size=640`, cropping enabled).
Deterministic Python code parses, validates, reconciles,
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
pages2md paper.pdf scans/
pages2md --force paper.pdf
pages2md --ignore-embedded-text poor-text-layer.pdf
```

The input is one supported paginated image source or a directory containing
images. OCR, layout, chapter detection, and quality settings are fixed. Output is written
beside the input after replacing a file extension with `.md`: `paper.pdf`
becomes `paper.md`, while the image directory `scans/` becomes `scans.md`.
Existing output requires `--force`.
If an input fails, the remaining inputs are still attempted and the command exits
with a nonzero status.

Within canonical Markdown, harmless OCR whitespace in LaTeX math is normalized
without removing grouping braces or changing operator and comma spacing.

Use `--ignore-embedded-text` for scans with a missing, stale, or low-quality
text layer. This disables embedded text blocks, character/font repairs, text
comparisons, and annotation-derived links. Rendering, OCR, PDF metadata and
outlines, and geometry-matched embedded image objects remain enabled. The
setting is part of the assembly fingerprint and, for the production backend,
the OCR evidence policy. Older image-only observations remain reusable when
guidance is enabled: the new decoder is used for new invocations, not silently
rerun over old checkpoints. Conversely, a workspace that may contain
native-guided observations cannot be reused for an image-only conversion.
The incompatible workspace is retained, and the command reports the conflict.
Turning off reconciliation cannot undo source influence during generation.

## Decode guidance and recovery

New invocations retain the reference exact n-gram hard mask and add bounded,
soft candidate scoring (`source-progress-v1`), without changing model weights:

- Positioned native text is assessed per region, independently of the OCR
  being corrected. Words and numeric values retain their identities; emitted
  occurrences advance local cursors. Grounding boxes and page markers select
  local evidence. Positive bias starts only after a matching grounding box;
  an ungrounded prefix cannot advance native cursors or steer into later text.
  The next 12 occurrences provide limited skips, with two
  initial region alternatives for reading order. This is not full beam search.
- Prose prefixes receive at most a 1.5-logit bonus. The processor considers the
  model's top 16 tokens plus tokenized local source continuations, so source
  suggestions need not already be in top-k. Geometry-free, ignored, malformed,
  and unsupported regions abstain. Math-only regions may corroborate source
  progress but do not bias the decoder toward flattened native math.
  Variables, numerals, uppercase acronyms, and math operator names form bias
  boundaries even before a LaTeX opener has been emitted. Candidate lookahead
  cannot jump across these boundaries to later prose.
- Sustained phrase/formula cycles receive a soft penalty. Only explicit
  counters are abstracted for detection; emitted indices are never rewritten.
  Tables and grounding coordinates are excluded. Recent source progress
  suppresses the loop penalty, allowing genuine repeated source occurrences.
  Prefixes shorter than 512 characters are not structurally penalized: a live
  regression showed that disturbing a short self-recovering prefix can prolong it.
- EOS is never forbidden. A small penalty applies only while substantial
  native text remains and alignment is active. After at least 512 generated
  tokens, a source-unsupported loop persisting for a further 256 tokens stops
  with `finish_reason=repetition_guard`. Short bad prefixes can still recover.
  The pipeline then tries an independent visual recipe where available; it
  does not repeat an identical deterministic one-page Base invocation.

Each observation records its actual visual contract and decoding diagnostics:
eligible/disabled regions, matched occurrences, bias steps, and policy version.
Selected-token confidence describes the **post-constraint decoder distribution**,
not calibrated transcription correctness. Guidance state is fresh for every
invocation; cache rollback requires a new guide, rather than reusing consumed
source state.

Recovery preserves immutable raw attempts. An unsupported ungrounded prefix
can be excluded from a derived candidate when at least two grounded body
blocks have source support. A coherent page-local candidate can replace even
a short or stop-terminated hallucination when both its source precision and
coverage improve sufficiently. Failed whole-page candidates are not adopted
into empty pages; independently corroborated valid regions may still be used.
Unresolved, source-unsupported long canonical loops fail bundle verification;
warnings from rejected attempts alone do not fail a recovered page.

Development tools help evaluate changes without overwriting the corpus:

```sh
PYTHONPATH=pages2md/src python pages2md/scripts/replay_decoding.py paper.pages2md --pages 1 5
HF_HUB_OFFLINE=1 PYTHONPATH=pages2md/src python pages2md/scripts/smoke_guidance.py page.png paper.pages2md/pages/page-0001.json --max-tokens 256
```

The first only replays saved observations. The second compares exact-n-gram,
structural-only, and native-guided decoding on the same image, with a bounded
token budget and stdout output. Embedded agreement is not independent ground
truth: accuracy must be assessed against source renderings. These mechanisms
do not yet implement alternative-beam model scoring, geometric PDF-to-LaTeX
decoding, crop rollback, or model fine-tuning, and cannot guarantee recovery.

Single-page calls automatically retry failed, ungrounded decodes with a constrained
initial `<|det|>` marker. This steers the model into its grounded output format;
region type, coordinates, content, and subsequent EOS remain model predictions.
It composes with the exact
n-gram constraint and available embedded guidance. A usable grounded body is
not regenerated merely to remove an ungrounded prefix: broader tests found
that unconditional prefix forcing can regress names and caption extraction.
At most one startup rescue is attempted, with both raw attempts retained.
An internal 4,096-token ungrounded-start budget prevents spending the full page
budget on startup failure; already-grounded long pages retain their full budget.
Multi-page starts are unchanged, and the pipeline skips visually blank pages
before calling OCR. Forced prefix
token counts and policy are recorded separately in decoding diagnostics; their
post-constraint confidence is not evidence of transcription accuracy.

The decoder also enforces incremental grounding-header syntax and valid ordered
coordinates,
tracks recent regions, and softly discourages suspicious same-region or
compressed nearby copies. This automatic policy applies to single-page greedy
decoding. Unlike the soft
source-progress guide, the header grammar excludes EOS inside an unfinished
header; it does not constrain mathematical content or forbid EOS in the body.
Tables and similar formulas in distinct regions are not blanket-banned.

If a completed near-duplicate remains, at most two fresh generations replay the
exact token prefix and explore another continuation at the suspect block's body.
Selection requires fewer detected duplicates, retained predicted-region coverage,
no additional math syntax errors, and a bounded likelihood loss. These are
heuristics, not independent source verification. All raw alternatives and
selection diagnostics are retained; an unhelpful retry leaves the original
unchanged. Selected replay candidates omit confidence summaries/spans because
forced-prefix probabilities are misleading. Decoder selection and the bounded
retry budget are internal policy, not user configuration: there are no decoder
CLI flags or constructor options. Developer benchmarks use private ablations.
See [automatic-decoder validation](experiments/automatic-decoding.md) for the
broader tests, measured costs, and remaining errors.

Recovery also checks local prose coverage inside predicted boxes, so an oversized
box cannot conceal an omitted paragraph. Ligatures are normalized and flattened
math identifiers are excluded from this prose test. Whole-page and local Detail
selection must preserve corroborated Base prose; a higher global score is not
enough to discard a good region. See [coverage and window validation](experiments/coverage-recovery.md).

Small disputed display equations can receive up to two additional, visual-only
region reads per page, each capped internally at 4096 tokens. Padding avoids
neighboring blocks. A crop is adopted only when a complete expression agrees
with an independent page-level read; crop coordinates are never used as page
coordinates. All alternatives, crop geometry and selection provenance are kept.
Ambiguous results leave the existing transcription unchanged. Two adjacent
Latin-for-Greek math errors can also be repaired from uniquely anchored native
glyphs with valid geometry, preserving the existing TeX structure.
See [rerun readiness validation](experiments/rerun-readiness.md).

Repeated reads of the same physical text region are checked across the whole
page, not only within a short decoder window. They trigger independent page
recovery and fail verification if they survive assembly; high embedded-vocabulary
agreement does not excuse duplicated content. Source-corroborated openings can be
preserved when a clean recovery clips the first line.

New decoder policies apply to new OCR invocations. Existing raw observations
remain reusable across these policy changes, with their original diagnostics;
source, model, rendering and embedded-text compatibility checks still apply.
Updating the decoder does not silently rerun the corpus.

`scripts/benchmark_decoding.py` compares bounded variants on production-resolution
renders and saves immutable-per-configuration raw outputs, hashes, diagnostics,
and source/code provenance under an explicit output directory. It refuses a
directory with a mismatching fingerprint. See [the hard-page experiment report](experiments/README.md)
for measured results, equation errors, and reproduction commands. These tests
neither deploy the decoder nor invalidate/reprocess existing corpus bundles.

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

## Implementation boundaries

Raw OCR observations are immutable evidence. Editable content lives in page
blocks and structured list-item bodies; rendered Markdown is derived from them.
`lists.repair_text_leaves` runs repairs on detached text leaves and commits only
Markdown and metadata after success, then renders list containers. Block kinds
and geometry are context, not editable structure.

- `pipeline.py` owns conversion, checkpointing, and publication.
- `document.py` owns document-wide normalization and link application.
- `reconciliation.py` explicitly sequences math repairs per block, reusing
  alignment until an edit changes the text; `alignment.py` owns occurrence
  matching, font decoding, and glyph relationships. Structural edits require
  native baselines; incomplete legacy geometry cannot establish a script.
- `syntax.py` owns source-mapped math and protected Markdown ranges, sharing
  parsed blocks and math spans within each analysis.
- `edits.py` applies source-offset changes and records their evidence. Conflicting
  edits within a batch abstain; no-op proposals cannot block real changes.
- `formatting.py` protects literal math and local footnotes before formatting
  prose. Preservation checks remain a safety net, not the normal formatting path.

Keep regression coverage for alternate math delimiters, protected syntax,
idempotence, repeated glyph occurrences, and cache-only reassembly. Changes to
deterministic processing invalidate assembly, not the retained model observations.
