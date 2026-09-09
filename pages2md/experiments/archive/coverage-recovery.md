# Coverage-aware recovery, 2026-09-09

## Policy changes

- Meaningful native prose must have local lexical coverage, not only a containing
  predicted box. Identical prose elsewhere on a page does not cover the region.
- Normalize Unicode ligatures. Exclude mixed-case flattened math identifiers
  such as `degX` from the prose test; geometric coverage still applies to math.
- Whole-page selection preserves at least 90% of locally corroborated Base prose
  occurrences in each supported region. Otherwise existing local reconciliation
  remains available. The same veto protects local replacements and span merges.
- No public configuration flags, no raw checkpoint invalidation, no corpus writes.
- Short unsupported alphanumeric prefix chains are excluded only from derived
  output with two corroborated grounded body blocks; raw evidence is retained.
- Local span edits must not introduce new math syntax errors, individually or
  in combination. Prefix exclusion now supplies verifier-compatible provenance.

These remain heuristics, not a guarantee of complete or mathematically correct OCR.
The change is at the automatic recovery/selection boundary, not a new logits bias.

## Validation

436 tests passed with the local MLX runtime and KaTeX 0.18.4. Nine new regressions
cover oversized boxes, actual completeness, wrong-region text, lost local prose,
math abstention, ligatures, whole-page selection losing a supported region,
short-prefix preservation/provenance, and math-safe span splicing.

Retained artifacts (gitignored):

- `artifacts/coverage-windows-01`: three actual two-page model calls, six rendered
  pages, raw generations and code snapshots. BCIKS20 60-61 and BCGM25 44-45
  segmented correctly; GaoKL24 10-11 emitted a single repetitive segment.
- The initial harness zipped pages with returned segments and therefore omitted
  GaoKL24 11. This is a harness defect, not evidence of successful recovery.
  The harness now uses production alignment. `replay_windows.py` exercises the
  full production candidate collection against retained raw data, generating
  only missing independent reads.
- `artifacts/coverage-windows-replay-01`: final-policy replay and recovery results.
- `artifacts/coverage-document-canary`: unmodified worktree copy of KKH26 (16 pages)
  passed to the no-flags CLI, with normal 32768-token budget and 300-DPI rendering.

The earlier window snapshots precede the ligature adjustment; replay snapshots
include that adjustment but precede the canary-driven prefix and splice fixes.
The canary was started after the ligature adjustment; `final-code` snapshots
record the completed implementation.

### Completed canary

All 16 pages processed. The first publication attempt failed: an unsupported
short `2D-2D-...` prefix survived, prefix-exclusion provenance lacked the verifier's
required fields, and independently balanced Base/Detail citation variants were
partially spliced into an unbalanced math expression on page 4.

After the fixes above, a no-flags CLI retry reassembled existing checkpoints and
published successfully (exit 0). Independent bundle verification passed. All 20
raw observation files predate that retry; no new raw OCR was generated. The
invented prefix is absent and math syntax errors are empty. Seven prose probes
on pages 1, 8 and 16 pass case-insensitive matching (two headings changed case).
The original failed assembly and final machine-readable verification are retained.

### Remaining limitations / rerun decision

The production-path replay covers all six physical window pages, including Gao's
missing second segment. BCIKS20 61 retains 6/6 checked anchors and no coverage gap;
GaoKL24 11 has no coverage gap. Four other pages still trigger coverage heuristics;
these are not independently certified omissions. BCGM25 45 retains 4/4 prose
anchors but its grouped-read final bound is mathematically wrong. A Detail read
puts multiple equations in one inaccurate bounding box, preventing safe local
selection of its improved bound. This is a remaining split/merged-region issue.

Publication success is not mathematical fidelity: the KKH26 abstract still has
an asymptotic-symbol transcription error. Lint and embedded-disagreement warnings
also remain. Do not describe the decoder as fully corrected or run the entire
corpus on the assumption that all repetitions and math errors are solved.

## Bounded mathematical-guidance experiment

`probe_math_frontiers.py` uses prefix-only glyph alignment on retained formula
reads. It requires six contiguous uniquely occurring matched glyphs before
proposing the next geometric glyph. It never changes OCR or uses a completed
formula to choose the frontier. Results are proposals, not accuracy measurements.

On the initial retained set: 16 formulas, 332 sampled prefixes, 75 eligible
frontiers; 220 unaligned, 23 noncontiguous, 13 ambiguous, and one at the end.
Even eligible frontiers can be wrong TeX continuations: after `\\frac` on BCIKS20
61, geometry proposed a later baseline `a` rather than the numerator. Other
proposals occur at closing delimiters. Thus this rule is **not enabled** as a
positive decoding bias. It needs TeX-parser state and fraction/script subtree
alignment before a controlled model A/B would be justified.

## Scope

PDF images were inspected using the production PyMuPDF renderer; Poppler was
avoided because the preceding experiment exposed broken local fontconfig setup.
No source PDF or existing corpus OCR was modified. Raw experiments are retained.
