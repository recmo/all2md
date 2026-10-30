# Automatic decoder validation - 2026-09-09

The decoder is now hands-off: no decoder CLI flags or constructor policy
options. This is a worktree implementation, not an installed-service deployment
or a corpus reprocessing run. Source PDFs and existing Better.codes OCR bundles
were read-only throughout.

## Decision from the expanded tests

**Enable header grammar and bounded block recovery automatically. Reserve
forced initial grounding for failed startup, not every single-page call.**

The first 20-page comparison isolated the block safeguards against an already
grounded baseline with embedded guidance enabled. Eighteen outputs were
byte-identical; BCGM25 p45 lost its spurious probability block and Jo26 p10 got
a valid coordinate header. No other page triggered a block retry.

But comparison with the previous default exposed a problem with unconditional
initial grounding: it changed otherwise usable text and layout. Visual QA,
following the PDF skill, was necessary to distinguish these changes from
improvements rather than treating native agreement or zero warnings as truth:

- BCGM25 p1 changed the correct `Kopparty` to `Koppart`.
- ABF26 p54 changed `Jörn` to `Jorn`. The source actually says `Jørn`, so both
  readings are wrong; this is a character-fidelity warning, not a correct-to-
  incorrect baseline example.
- GaoKL24 p11 stopped emitting the two panel captions as text and instead
  enclosed them in a larger image region, alongside four nested panel images.
  The text remained in the proposed image crop, but searchable caption
  extraction regressed and redundant nested regions appeared.
- Other differences included typographic quotation marks, formula formatting,
  recovery of a section heading, and a wrong logo reading becoming `[Non-Text]`.
  These are not all regressions or all improvements.

The resulting adaptive policy reproduced **18 of those same 20 previous-default
raw outputs byte-for-byte**. Only the targeted duplicate and malformed header
changed. In particular, it retained the original names and panel-caption text.
This does not make the retained OCR correct: previous prefixes, symbol mistakes,
and other defects are also retained for the existing reconciliation/recovery
pipeline to handle.

## Automatic behavior

1. The existing visual blank-page detector runs before OCR.
2. Single-page greedy calls use the grounding-header grammar and region-aware
   copy scoring. Multi-page calls keep their existing decoding contract.
3. A suspicious completed duplicate can trigger up to two fresh exact-prefix
   replay alternatives. Coverage, syntax, likelihood and natural-stop gates
   select an improvement or retain the original.
4. A failed ungrounded startup gets at most one initial-marker rescue. Failure
   means no grounded body plus a token/guard stop, a structural loop, a short
   ungrounded stop, or substantial ungrounded prose with only marginal regions
   such as a page number. A real page-number-only output is not enough to retry.
   The rescued candidate must stop naturally with grounded content and without
   a detected structural loop or block duplicate. A constraint conflict or
   unhelpful result retains the original. Its normal block retry remains bounded.
   An internal 4,096-token startup guard applies only while there is no grounded
   body and no forced-prefix rescue is already underway. Grounded long pages
   retain their full token budget. The stopped attempt is retained with
   `finish_reason=ungrounded_guard` and is treated as truncated if rescue fails.
5. A grounded but incomplete page remains eligible for the existing pipeline's
   independent Detail recovery; mere existence of a bad prefix does not cause
   the backend to regenerate a usable body unconditionally.

Both startup and block alternatives remain in observation diagnostics, including
raw text, selected attempt and total generated-token cost. Block-replay
selection omits misleading confidence summaries/spans. No weights or prompt
transcriptions are changed. Native prose guidance remains complementary; this
does not add geometry-backed native-math decoding.

The constructor exposes no policy switches. Private benchmark ablations permit
controlled comparisons, but normal use remains `pages2md document.pdf`.
Decoder-policy changes do not invalidate expensive old raw observations.
Source, model revision, precision, render DPI, token budget and native-influence
compatibility are still enforced. Tests exercise actual checkpoint reassembly
after both assembly and decoder-policy changes without additional OCR calls.

## Corpus sample

Physical PDF page numbers, selected for layout diversity and known failures:

| Document | Broad comparison pages | Extra original startup failures |
| --- | --- | --- |
| ABF26 | 1, 2, 3, 35, 54, 55 | 25 |
| BCGM25 | 1, 2, 30, 45, 58 | 40 |
| Jo26 | 2, 10, 18, 19 | 1 |
| Jer26 | 1, 2, 19, 37 | - |
| GaoKL24 | 11 | - |
| BCIKS20 | - | 61 (printed page 60) |

These cover title/abstract pages, contents, prose with footnotes, derivations,
references, sparse final pages, tables and figure panels. Full-page layout
previews were inspected, with higher-resolution checks of changed source names,
the final probability bound, table rows and figure captions. This is not an
exhaustive proof or transcription audit.

The final hard-page retest recovered all seven probes on Jo26 p1 through the
footer-only startup rescue. BCGM25 p40 and ABF26 p25 also recovered all seven
probes each through startup rescue. BCIKS20 p61 retained only five of six probes
in Base, but its independent Detail call recovered all six. Detail is not
uniformly better: on Jo26 p1 it retained only six of seven probes, missing the
author, while Base retained seven. Candidate selection matters as much as
availability of an alternative.

All runs used the cached pinned model, production 300-DPI rendering, greedy
decoding and a 4,096-token per-invocation cap. Both the broad comparison and the
adaptive run supplied positioned native evidence; raster controls supplied none.
Untuned ordinary pages have no manually selected anchors: their probe recall is
`null`, not a fictitious perfect score. Raw hashes and body differences were
compared separately.

On BCGM25 p45, adaptive block recovery reduced 1,359 tokens / six formula regions
to 1,120 tokens / five formula regions. The first alternative emits the correct
final `n_out * epsilon_MCA(gamma)` bound in place of the extra probability block,
while retaining the original prefix and preceding formula tokens. This took
18.676 seconds versus 8.594 seconds for the prior-default run, not a calibrated
throughput comparison. Every unrelated broad-comparison page avoided replay.

## Raster and end-to-end controls

`benchmark_layouts.py` creates deterministic white/off-white blank pages, a
figure-only page, a table-first page with five repeated data rows, sparse text
with a page number, two identical paragraphs in distinct regions, and two
columns. It compares Base and Detail calls plus a two-page columns/table call.
The repeated text is literal repeated source content, not a loop to remove.

Under the adaptive policy, both blank controls bypass OCR and the multi-page
output is byte-identical to the previous policy, with two correctly separated
pages. Both legitimate paragraph occurrences and all five table rows are
retained. The failed table-first Base startup is recovered automatically.
Sparse-page text and page number remain present.

There are still isolated-method failures: Detail can miss the left column of
the synthetic two-column page, and figure-only raw output can contain redundant
image regions. The adaptive policy deliberately does not claim these are fixed
by grammar alone. Normal conversion begins with Base; figure deduplication and
independent recovery are separate pipeline responsibilities.

The final no-flags CLI retest used the normal 32,768-token full-page budget with
the internal startup guard. It published BCIKS20 p61 with all six probes,
BCGM25 p45 with all four probes and the corrected final bound, and the table
control with all five data rows. All 14 probes passed in the published canonical
Markdown, not just a rejected alternative. The three-input command completed
in 83.10 seconds including model loads and publication. BCIKS20's Detail attempt
stopped at 4,096 tokens with `ungrounded_guard`, selected its 1,648-token rescue,
and the pipeline included the previously absent opening paragraph.

Earlier final-policy CLI controls retained both copies of the repeated paragraph
and skipped the blank image. Figure-only publication kept the whole diagram and
three nested crops; this redundancy remains, rather than being reported as a
clean single-figure result. BCGM25 still has an unrelated double-subscript KaTeX
finding. Preserved-attempt warnings and transcription warnings are not erased
merely because publication succeeds. `verified-summary.json` in the bounded
CLI artifact directory records canonical hashes/probes and final code hashes.

## Reproduction and evidence

Retained gitignored directories under `experiments/artifacts/`:

- `broad-native-wave01`: 20 pages, already-grounded baseline versus block grammar
  and recovery, both with native guidance (40 invocations plus one block replay).
- `broad-previous-default`: the same 20 pages with the earlier default startup.
- `automatic-adaptive-wave01`: 20 comparison pages plus four startup failures;
  exposed the footer-only Jo26 case, subsequently fixed and retested.
- `automatic-final-hard`: final Base/Detail checks on BCIKS20 p61, Jo26 p1 and
  BCGM25 p45.
- `layout-controls-wave01`: unconditional-startup experiment, retained as
  evidence rather than silently replaced by the chosen adaptive policy.
- `layout-controls-adaptive`: adaptive Base/Detail and multi-page controls.
- `automatic-cli-smoke`: initial unconditional-policy CLI experiment.
- `automatic-cli-final`: no-flags CLI using the adaptive production defaults,
  with copied raster controls and hard-page images, not original corpus files.
- `automatic-cli-bounded`: final no-flags retest after adding the internal
  ungrounded-start budget; previous experimental outputs remain intact.

The final unit/integration suite passed **427 tests**, no skips, with five SWIG
deprecation warnings. New cases cover rejection of dropped regions, syntax
regressions, low-likelihood alternatives, changed prefixes, truncated attempts,
fresh-state conflicts, bounded startup rescue and footer-only failures, genuine
page-number-only output, no public policy options, checkpoint reuse, and stopping
an ungrounded startup without capping a grounded page longer than 4,096 tokens.

Each model benchmark retains source/image hashes, raw text, diagnostics, and
decoder/harness snapshots. Use a fresh output directory after code changes.
For example, from the repo root in the OCR environment:

```sh
HF_HUB_OFFLINE=1 PYTHONPATH=pages2md/src python pages2md/scripts/benchmark_decoding.py \
  --corpus /Users/remco/Documents/Better.codes \
  --output pages2md/experiments/artifacts/new-automatic-run \
  --cases ABF26-1 ABF26-54 BCGM25-1 BCGM25-45 Jo26-10 Jer26-37 GaoKL24-11 \
  --variants base-guided base-grounded-guided base-automatic --max-tokens 4096

HF_HUB_OFFLINE=1 PYTHONPATH=pages2md/src python pages2md/scripts/benchmark_layouts.py \
  --output pages2md/experiments/artifacts/new-layout-run
```

## Limits

These are bounded engineering checks, not a corpus-wide false-positive estimate.
Geometry is model-predicted, prose anchors measure only coarse coverage, and
mathematical symbols/indices remain fallible. BCIKS20 p61 still misses its opening
probe in isolated adaptive Base mode; a partial page must go through pipeline
recovery. The safeguards do not remove every existing raw prefix or repair every
formula. The hands-off requirement is met by internal policy and bounded
fallbacks, not by assuming every OCR result is accurate.

The first full-budget CLI run exposed a real failure hidden by the small-budget
benchmark: BCIKS20 Detail continued into a hallucinated grounded table before
the 32,768-token limit, bypassed startup rescue, and the pipeline published the
partial Base result. That motivated the internal 4,096-token ungrounded-start
guard, using the already tested rescue point. CLI exit zero alone was not
counted as successful transcription. Nested figure crops and unresolved math
errors also remain visible in the full-pipeline results.
