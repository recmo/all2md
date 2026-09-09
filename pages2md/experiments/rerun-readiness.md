# Targeted region recovery and rerun readiness

The generation results below predate the maintenance cleanup. After removing
production ablation switches and fixture-only backend fallbacks, the full suite
still passed (472 tests). Subsequent review fixes protect detected figures,
reject failed confirming reads, preserve changed/merged canonical equations,
and reject stale benchmark source images. The current branch has 489 passing
tests; this is not validation of an integrated main build.
Historical runners are archived; current tools are
listed in [the validation index](README.md). No new model reads were needed for
that cleanup.

**Status: integrated validation in progress; not yet cleared for a full rerun.**
The earlier document canaries (35 physical pages total) predate the merge with
main. Fresh integrated canaries are still required. The original corpus OCR
has not been modified, and the full rerun has not started.

## What changed

The decoder now has an automatic local visual recovery stage for small disputed
display equations. This addresses a failure that page-level candidate scoring
cannot fix: the improved equation may occur inside a merged block whose predicted
bounding box only covers the first equation.

- At most two short disputed display equations per page; agreed regions, tables,
  and paragraphs are not subjected to this new crop policy.
- Visual-only crops, with neighboring blocks limiting vertical padding. Canvas,
  pixel/page geometry, image hash, original raw text and confidence are retained.
- At most 4096 tokens per region attempt, without reducing the ordinary page
  budget. Existing bounded startup/block retries still apply inside an attempt.
- Adopt exactly one complete math expression only when an independent page read
  corroborates it. A matching substring inside a larger expression is insufficient.
- Preserve script direction, operators and mathematical font roles in comparison.
  Only harmless spacing, delimiter sizing and text-label style are normalized.
- Retain the page block's geometry and surrounding content. Crop observations
  are excluded from ordinary whole-page and spatial candidate selection.
- Failed or ambiguous reads leave the original intact. One failed crop does not
  discard another completed crop. Identical text at different targets gets
  separate evidence identities.
- Whole-page same-region repetition is checked beyond the decoder's short block
  window. Repeated text at different positions is preserved. A duplicated region
  triggers an independent page-level Base recovery even if Detail is syntactically
  clean; a surviving duplicate is a publication error, not excused by vocabulary
  agreement with embedded text.
- An accurate, source-corroborated opening can be preserved from the original
  visual read when a clean page recovery clips or guesses that opening. This is
  restricted to the narrow row band before the first grounded recovery block.

The native-text repair additionally handles a tightly constrained two-glyph
Latin-for-Greek error, with four already matched glyphs on each side, unique
occurrence context, matching case, valid geometry and math-only source ranges.
It changes identities, not TeX structure. Positive next-glyph math steering
remains disabled: the earlier fraction-prefix experiment showed it is unsafe.

No public decoder options, model-weight changes, deployment, or corpus rerun.
Raw checkpoint compatibility remains unchanged; new stages apply to new reads.

## Targeted experiments

All directories below are retained under gitignored `experiments/artifacts/`.

- `region-crops-01`: ten Base/Detail reads of five BCGM25 45 equations. Several
  broad crops produced loops or malformed structures; this ruled out blanket
  crop replacement. The small final-bound crop was correct.
- `region-policy-01`: production policy on BCGM25 44-45 and BCIKS20 60-61.
- `region-policy-final`: repeated targeted reads with the final 4096-token cap.
  Four region calls produced the same three selections: BCGM25's final bound,
  BCIKS20's `Q(X,Y)` definition (`l`, not `t`), and a second displayed formula.
  BCGM25 44 abstained. BCIKS20 60 triggered no small-region reads.
- `region-canary-01`: separate copy of the 16-page KKH26 canary, augmented with
  fourteen targeted region observations. All twenty previous raw observations
  were hash-verified unchanged. Four region selections passed visual inspection.
  Reassembly and publication succeeded; independent verification has no surviving
  decode or math-syntax errors. The abstract now has `2^{Ω_ρ(1/η)}`, and the
  unsupported `2D-2D-...` prefix remains absent. Raw observations total 34.
- `region-fresh-canary`: a fresh no-flags conversion of all nineteen Jo26 pages.
  The initial run published but the content audit found a duplicated section on
  page 16. Its observed runtime was 752.528 seconds, with other experiments sharing
  the machine; this is not an isolated throughput benchmark.
- `region-final-canary`: all nineteen Jo26 pages rebuilt from the first run's
  forty-five preserved raw observations plus one independent page-16 Base read
  (1322 tokens). The winning derived page uses the clean Detail body and preserves
  the original accurate opening. Corollaries 5.9 and 5.10 now occur once each;
  page 16 shrank from 5722 to 2847 characters without losing its opening or ending.
  Publication and independent verification pass, with no surviving region-repeat
  or math-syntax errors. All forty-five earlier raw hashes match; total raw count
  is 46. Both the initial duplicated publication and the intermediate opening
  variant are retained rather than overwritten.
- `readiness-final`: final-code reassembly of the four hard pages confirms the
  corrected BCGM25 bound and BCIKS20 exponent. The strengthened verifier explicitly
  rejects the original duplicated Jo26 artifact, demonstrating that this failure
  can no longer receive the earlier false-green verification result.

The first canary/crop runs predate the later budget/identity/failure-isolation
hardening. `region-policy-final` tests the bounded final OCR entry point.
Audit code snapshots describe deterministic verification/reassembly, not a claim
that earlier generations were rerun under newer code.

## Readiness boundary

Readiness means that known blocking recovery failures are addressed, regression
controls pass, complete document canaries publish, and no known catastrophic
repetition or missing-page failure remains in those canaries. It does not mean
that every mathematical symbol in the corpus is certified correct. Native
disagreement, uncertain OCR and Markdown lint warnings still require honest
reporting; mathematical use of the transcription should retain source access.

The final suite has 472 passing tests (five existing SWIG deprecation warnings).
This includes crop budget/identity/failure isolation, image-integrity checks,
two-glyph repair abstention, legitimate repetition controls, long-distance region
duplication, automatic independent rereading, and opening preservation. Visual
checks covered the selected equation crops and complete relevant source pages.

A full rerun should use fresh staged bundles, preserving previous corpus OCR for
comparison. Updating the code alone intentionally does not invalidate old raw
checkpoints. Do not use destructive replacement merely to obtain a new decode.
