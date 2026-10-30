# Hard-page decoder experiments - 2026-09-08

Current follow-up: [automatic decoder validation - September 9](automatic-decoding.md).
The older opt-in descriptions below are historical; decoder policy is now
automatic with no user-facing decoder settings.

Status: promising single-page recovery, not validated mathematical transcription
or a corpus-wide rollout. All PDF/OCR inputs were read-only. Outputs are retained
under the gitignored `artifacts/` directory in this worktree. No model weights,
installed service, source PDF, or existing Better.codes bundle was changed.

Follow-up: [12 additional hard pages](other-hard-pages.md) confirm startup-loop
recovery but expose a residual near-duplicate grounded equation block that the
current repetition detector misses. Initial grounding is not a complete solution.

Implementation follow-up: [grounded block decoding](block-decoding.md) adds opt-in
header grammar, geometry-dependent copy scoring, and bounded boundary retries.
It fixes the observed BCGM25 p45 duplicate and Jo26 p10 malformed header, with
four byte-identical controls; mathematical transcription errors remain.

## Setup

- Unlimited-OCR pinned model revision `07dea832e22aefee32ad281d4b80551282e1c168`,
  cached offline, MLX on local Apple Silicon.
- Production PyMuPDF rendering at 300 DPI, deterministic greedy decoding,
  4,096-token cap per invocation. Same image for every variant within a case.
- Base: 1024 uncropped; detail: supported 640 cropped, 1024 base size.
- Baseline retains the reference exact 35-token n-gram constraint; this is not
  an unconstrained baseline. Guided variants add the current structural/native
  soft guidance and stream guard.
- Grounded variants constrain only the initial `<|det|>` token sequence. They
  contain **no source transcription, label, bounding box, or answer hints**.
- Raw outputs and selected-token diagnostics are saved before reconciliation.
  Native-token agreement measures alignment, not accuracy. The prose anchors
  are coarse coverage probes, not CER/WER or full-page correctness.
- Earlier low-resolution smoke previews did not reproduce these production-
  resolution failures and should not be compared as if inputs were identical.

## Wave 1: four hard pages, six variants

Sources are physical PDF pages in `/Users/remco/Documents/Better.codes`.
BCIKS20 physical page 61 is printed page 60.

| Page | Base exact failure | Base guided | Detail exact | Base grounded | Detail grounded |
| --- | --- | --- | --- | --- | --- |
| BCIKS20 61 | 4,096 tokens, no grounded content | Recovers after 1,272-character bad prefix, 5/6 anchors | 4,096 tokens, no grounded content | 1,633 tokens, 6/6 anchors | 1,648 tokens, 6/6 anchors |
| BCGM25 40 | Stops after 49 tokens of repetition | Same failure | 7/7 anchors, 66-character prefix | 1,587 tokens, 7/7 anchors | 1,587 tokens, 7/7 anchors |
| ABF26 25 | 4,096 tokens, malformed ungrounded math | Same failure | 7/7 anchors, damaged opening theorem | 741 tokens, 7/7 anchors | 740 tokens, 7/7 anchors |
| Jo26 1 | Stops after 56 tokens; misses title and abstract | Same failure | Bad prefix, missing author, 6/7 anchors | 740 tokens, 7/7 anchors | 736 tokens, 7/7 anchors |

Both grounded visual recipes start immediately with a detection marker, stop
naturally, find all 27 prose anchors, and have no parser repetition/truncation
warnings on these four pages. Base grounded took 5.7-9.2 seconds per page in the
first wave. Timings are single observations, include different warmup effects,
and are not a throughput benchmark.

Adding current embedded-text guidance to base grounded produced **byte-identical
raw output on all four pages**. Native bias was active on 89, 164, 117, and 145
steps respectively. This demonstrates composition without an observed change,
not an accuracy improvement from native guidance. Increasing its weight without
new evidence would risk overriding visual math.

## Wave 2: integrated implementation and controls

Eighteen additional calls tested base exact, base grounded, and detail grounded
on the four original pages plus BCGM25 page 4 and Jo26 page 3. All twelve repeated
hard-page raw hashes matched wave 1 exactly after moving the processor into the
backend. Thus the measured improvement survived integration, not just a harness
prototype. Total across both waves: **42 model calls on six distinct pages**.

| Additional page | Base exact | Base grounded | Detail grounded |
| --- | --- | --- | --- |
| BCGM25 4 | 1,415 tokens, readable, short spurious `1.2.` prefix | 1,411 tokens, same coarse coverage, no prefix | 1,418 tokens, same coarse coverage |
| Jo26 3 | 72 tokens, early stop, almost all content absent | 1,244 tokens, 16 blocks and 5 displays | 1,239 tokens, 16 blocks and 5 displays |

BCGM25 4 is the usable self-recovering control: all three have all three probes,
equal native-token recall, and no repetition/truncation warnings. Grounding also
changes bounding boxes, so this is a coverage check, not a byte-identity claim.
The source page has no `1.2.` at its start. Jo26 3 was selected as a control from
its saved OCR but failed as an isolated production-resolution Base invocation;
it is another recovery result, **not** a successful-baseline regression control.
One usable control is insufficient to establish general non-regression.

## Visual equation spot-checks

The complete relevant source pages were inspected visually, then these specific
raw-output disagreements were checked against the rendered glyphs. This is not
a proof review or an exhaustive transcription audit.

| Source detail | Base grounded | Detail grounded |
| --- | --- | --- |
| BCIKS20 61: first paragraph uses `a_X`, not alpha | Wrong `alpha_X` | Correct `a_X` |
| BCIKS20 61: `X^{l^{(i)}}` in definition of Q | Wrong `X^{t(1)}` | Correct nested exponent |
| BCGM25 40: `(m+1/2)^7` | Wrong exponent gamma | Correct exponent 7 |
| BCGM25 40: domain D in Lemma 9.3 | Spurious bar on D | Correct D |
| ABF26 25: `delta_min` and numerator n in Theorem 5.3 | Wrong max and eta | Correct min and n |
| ABF26 25: Lambda in the first display | Wrong roman A | Correct Lambda |
| ABF26 25: epsilon subscript ca in Theorem 5.4 | Wrong alpha | Correct ca |
| ABF26 25: proximity-loss subscripts fld and int | Wrong ba and mt | int correct, **fld still wrong as fd** |
| Jo26 1: exact-transfer display uses `C^{equiv s}` | Wrong `C^{mathrm{ss}}` | Correct interleaving exponent |

Detail grounded is the better recovery candidate on these inspected math probes.
It still loses the `l` in `fld`, and prose/math font distinctions and proof-ending
squares are not all preserved. Zero quality warnings does **not** mean correct
math. The relevant distinction is successful page recovery versus symbol fidelity.

## Mechanisms supported by this experiment

1. **Ungrounded start / early EOS:** constrain the structural prefix before the
   attractor develops. This changes the actual autoregressive continuation; it
   is not an output rejector or deletion of a bad prefix after generation.
2. **Long formula/counter loops:** keep structural soft penalties and a bounded
   stream guard as secondary protections. They recovered one Base case but did
   not solve the other three by themselves here.
3. **Small glyphs, indices, and near-neighbor symbols:** use an independent
   cropped visual recipe. The supported cropped view corrected several concrete
   errors that prefix steering alone did not address.
4. **Embedded text:** retain local occurrence/geometry-aware guidance, with math
   abstention. No gain was measured here. A future experiment should compare
   geometry-backed math alternatives inside an already recognized formula, not
   force a flattened native string into the entire page.
5. **Candidate selection:** retain original attempts and compare local coverage
   and visual disagreements. Do not select solely on length, confidence, anchor
   coverage, or native-token overlap.

The reusable processor is opt-in via `initial_grounding=True`, off by default.
It applies to single-page base/detail calls only, refuses invalid tokenizer
markers, incompatible hard masks, multi-row hypotheses and rollback reuse, and reports forced
prefix tokens separately. Multi-page starts are untouched. Blank, figure-only,
and table-first pages need dedicated tests before any default activation.

## Reproduction and artifacts

From the repository root, with the OCR Python environment available:

```sh
HF_HUB_OFFLINE=1 PYTHONPATH=pages2md/src python pages2md/scripts/benchmark_decoding.py \
  --corpus /Users/remco/Documents/Better.codes \
  --output pages2md/experiments/artifacts/new-run \
  --cases BCIKS20-61 BCGM25-40 ABF26-25 Jo26-1 \
  --variants base-exact base-guided detail-exact base-grounded base-grounded-guided detail-grounded \
  --max-tokens 4096
```

Use a fresh output directory after changing code or decoding/render configuration.
Repeating the same run resumes completed variants by checking recorded hashes;
existing raw files are not blindly reused across configurations.

- `artifacts/hard-pages-wave01/`: original 24 calls, six variants on four pages.
- `artifacts/hard-pages-wave02/`: integrated-processor verification and additional
  controls; exact commands and configuration are represented by the manifest,
  saved harness, source snapshots, per-case source records, and result JSON.
- Each case contains `source-300dpi.png`, `source.json`, and per-variant raw `.txt`
  plus `.json` diagnostics and SHA-256. `results.json` is the selected run's index.
- Model and declared dependency revision, source hashes, renderer DPI, token
  budget, harness and decoder source hashes are in the manifest. Wave 2 also
  preserves the harness and relevant source code alongside outputs.

Unit/integration suite after adding the option: 377 tests passed, no skips.
Coverage includes marker-only forcing/release, fresh state, conflicting masks,
multi-page exclusion, opt-in identity/diagnostics, and existing decoder/pipeline
regressions. No corpus reprocessing or deployment was performed.
