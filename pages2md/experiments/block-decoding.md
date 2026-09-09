# Grounded block decoding — 2026-09-08

Historical experiment record. The following day's [broader validation](automatic-decoding.md)
made the policy automatic and removed public decoder options. The opt-in API
below describes the implementation tested on September 8, not the current API.

Status: implemented and tested in this worktree, opt-in and single-page only.
No model weights, installed service, source PDFs, or existing corpus bundles
were changed. This addresses grounding syntax and one observed near-duplicate
failure, not general mathematical transcription correctness.

## Mechanisms

`MlxUnlimitedOcr(block_decoding=True, block_retries=2)` enables
`grounded-blocks-v1`; the default remains off. The policy/version and retry
budget are included in backend identity. It implies initial grounding and
composes with the existing exact n-gram mask and optional native-text guide.

1. **Incremental header grammar:** require a block label and four integer
   coordinates with `0 <= left < right <= 1000` and
   `0 <= top < bottom <= 1000`, followed by the closing detection marker.
   Partial numbers must have a feasible completion. EOS is excluded only
   inside unfinished headers. Types and coordinates are still model choices;
   no source bounding box or transcription is injected. This is greedy-only:
   candidate inspection widens until a legal finite continuation is found.
2. **Region-aware copy scoring:** compare recent same-type blocks using a
   formatting-normalized fingerprint that preserves letters, numbers,
   operators, case, and subscript/superscript identity. Similar text alone
   is insufficient: boxes must overlap substantially or place a much smaller
   copy immediately below the prior block. Tables and page boundaries are
   excluded. During generation, a matching long prefix in suspicious geometry
   receives a soft continuation penalty, not a blanket content ban.
3. **Bounded boundary alternatives:** if a completed near-duplicate survives,
   replay the exact token prefix with fresh generation/processor state and
   exclude the explored token at the first substantive body position. At most
   two alternatives are generated; replay is a new prefill, not KV-cache
   rollback. The model chooses the replacement continuation.

A candidate must stop naturally, preserve the exact prefix, reduce duplicates,
retain at least 90% coverage of every non-duplicate original predicted region,
add no math syntax errors, and lose at most 1.0 in mean continuation log
probability. Coverage permits three normalized coordinate units of jitter.
Scores are measured after preceding n-gram/native processors but before block
constraints, and exclude the forced prefix and fork token. Neither likelihood
nor predicted geometry establishes source fidelity. Selection prefers fewer
duplicates, then higher mean likelihood, and stops on an eligible zero-duplicate
candidate. An unhelpful or constraint-conflicting retry retains the original.

Every raw attempt, score, selected index, excluded fork token, and total token
cost is retained in `decoding.block_retry`. A selected replay candidate omits
confidence summaries/spans: forced replay would otherwise inflate confidence.
Set `block_retries=0` to isolate grammar and live soft scoring.

## Six-page comparison

Same pinned Unlimited-OCR model and 300-DPI renderer as the earlier experiments;
Base 1024 uncropped, deterministic greedy decoding, 4,096 tokens per invocation,
offline cached weights. Baseline is `base-grounded` (including the exact n-gram
mask), not unconstrained decoding. Neither variant uses native guidance here.

| Physical PDF page | Baseline | Block decoder |
| --- | --- | --- |
| BCGM25 45 | 1,328 tokens; 6 formula regions, one near-duplicate | 1,093 tokens; 5 formula regions, duplicate absent; first retry selected |
| Jo26 10 | 1,199 tokens; malformed coordinate header | 1,200 tokens; valid header, coordinate warning absent |
| ABF26 35 | 1,672 tokens; table control | Byte-identical; all 13 data rows retained |
| Jo26 14 | 1,251 tokens; legitimate similar derivations | Byte-identical |
| GaoKL24 14 | 2,308 tokens; existing malformed math | Byte-identical; math error remains |
| BCGM25 23 | 2,171 tokens; existing malformed math | Byte-identical; math error remains |

All 33 prose coverage probes survive. These are coarse probes, not CER/WER or
proof of complete transcription. Full source-page previews were inspected,
with targeted comparison of the corrected bound/header and legitimate repeats
and table rows. The PDF skill's visual-QA procedure kept these checks separate
from token statistics and native-text agreement.

On BCGM25 p45, the alternative emits `<= n_out * epsilon_MCA(gamma)` in place
of the extra probability expression and does not emit a second final-bound
region. This matches the source's final bound. The existing earlier formula
errors remain. No expected equation was supplied to the decoder, and no
postprocessing deletion produced the result. **The boundary retry caused this
fix; the live soft-copy penalty recorded zero active steps on this sample.**
The retry uses 2,421 total generated tokens (1,328 + 1,093), for 15.616 seconds
versus 7.521 seconds baseline in this comparison. Single-run timings include
runtime variation and are not a throughput benchmark.

On Jo26 p10, the header changes from `[113-371, 784, 387]` to
`[113 , 371, 787, 387]`; the grammar prevents the malformed continuation.
The inspected paragraph is at the corresponding source location. This does
not establish exact bounding-box accuracy elsewhere.

An earlier experiment rejected the correct BCGM25 retry because two units of
vertical box jitter reduced unpadded overlap to 0.8684. The three-unit tolerance
addresses that failure without globally lowering the 90% coverage requirement.
Unit tests distinguish jitter from a genuinely missing region. This threshold
was tuned on the target page, not independently validated on a large corpus.

The final-code smoke repeated both target pages and reproduced their selected
raw outputs byte-for-byte. The full suite passed **404 tests**, no skips, with
five existing SWIG deprecation warnings. Tests cover grammar bounds/dead ends,
geometry-sensitive detection and soft scoring, preservation of legitimate
repeats, exact replay, fresh state, bounded selection/fallback, raw provenance,
confidence omission, opt-in identity, and multi-page exclusion.

## Reproduction and retained evidence

From the repository root, using the OCR Python environment:

```sh
HF_HUB_OFFLINE=1 PYTHONPATH=pages2md/src python pages2md/scripts/benchmark_decoding.py \
  --corpus /Users/remco/Documents/Better.codes \
  --output pages2md/experiments/artifacts/new-block-run \
  --cases BCGM25-45 Jo26-10 ABF26-35 Jo26-14 GaoKL24-14 BCGM25-23 \
  --variants base-grounded base-blocks --max-tokens 4096
```

Use a fresh output directory for changed code/configuration. Retained,
gitignored worktree artifacts include raw text, result/attempt diagnostics,
source hashes and renders, and exact decoder/harness snapshots:

- `artifacts/block-decoding-wave01/`: initial three-page comparison, including
  the correct alternative rejected by the pre-jitter coverage gate.
- `artifacts/block-decoding-wave02/`: six-page comparison above.
- `artifacts/block-decoding-final/`: final-code two-target smoke.

## Remaining limitations

This is a narrow heuristic, not a guarantee against all repetitions. Detection
uses only six preceding blocks, long bodies, and predicted geometry. Real
repeated content can resemble a failure; short loops and incorrect geometry can
escape detection. Four unchanged controls do not establish corpus-wide safety.
Blank, figure-only, table-first, and multi-page layouts need broader evaluation
before default activation. Full prefix replay adds substantial retry cost.

Geometry-backed embedded-math alternatives and targeted crop re-decoding remain
separate future experiments. Native prose guidance is still complementary;
flattened native math is not forced into formulas. This change neither enables
those mechanisms nor fixes remaining symbol, index, or LaTeX transcription
errors. No corpus invalidation, reprocessing, or deployment was performed.
