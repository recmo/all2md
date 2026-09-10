# Additional hard pages - 2026-09-08

The fixed grounding-start decoder was checked on **12 additional physical PDF
pages**, with 36 calls: base exact, base grounded, and detail grounded. Source
PDFs and existing bundles were read-only. Only the benchmark case list and this
report changed; decoder code and deployment settings did not change.

One subsequent native-guidance check on the residual BCGM25 page 45 failure
brings this follow-up to **37 model calls** in total.

Same conditions as the [initial experiments](README.md): offline pinned model,
production PyMuPDF 300-DPI images, greedy decoding, exact 35-token n-gram
constraint, 4,096-token cap, base 1024 uncropped and detail 640 cropped. Neither
grounded variant in the main batch uses embedded-text bias or the soft loop
guard, isolating the initial marker constraint from those mechanisms.

## Outcome

Both grounded variants stopped naturally on all 12 pages and found all **70/70
prose coverage probes**. Base exact stopped on 10/12 and found 58/70. The two
4,096-token baseline loops never reached the source content; both recovered
with grounding-start. Repetitive preambles also disappeared.

**This does not solve every repetition.** Visual review found an extra,
near-duplicate grounded equation block on BCGM25 page 45 in base grounded,
despite zero repetition warnings and full prose coverage. Detail grounded does
not contain that extra block. This is a counterexample to treating either the
opening marker or current quality warnings as a complete repetition solution.

## Per-page results

All page numbers below are physical PDF page numbers. BCIKS20 physical page 57
is printed page 56. Columns list newly generated raw outputs, not the previously
reconciled corpus Markdown.

| Page | Base exact | Base grounded | Detail grounded |
| --- | --- | --- | --- |
| ABF26 34 | Repetitive 265-character prefix, then page content | No prefix; 6/6 probes | No prefix; 6/6 probes |
| ABF26 35 | Table and prose recovered; 4-character extraneous prefix | 13 data rows preserved; 5/5 probes | Same rows/probes, but unbalanced braces in math |
| ABF26 45 | Repetitive 387-character alpha/matrix prefix; detector misses it | No prefix; 7/7 probes | No prefix; 7/7 probes |
| Jo26 10 | Repetitive 143-character prefix and negative coordinate | Prefix gone, coordinate still negative; 6/6 probes | Same remaining coordinate defect; 6/6 probes |
| Jo26 11 | Repetitive 212-character mu prefix and spurious top page number | Both spurious prefix/top number gone; 6/6 probes | Same recovery; 6/6 probes |
| Jo26 14 | 4,096-token numbered-alpha loop, no grounding, 0/6 probes | Stops at 1,251 tokens; 6/6 probes | Stops at 1,263 tokens; 6/6 probes |
| GaoKL24 5 | 4,096-token alpha counter loop, no grounding, 0/6 probes | Stops at 1,153 tokens; 6/6 probes | Stops at 1,159 tokens; 6/6 probes |
| GaoKL24 14 | Content present but malformed math; 3-character extraneous prefix | Still malformed math; 6/6 probes | No brace/environment warning, but symbol errors remain; 6/6 probes |
| BCGM25 23 | Repetitive 318-character R-metric prefix, malformed math | Prefix gone, math warning remains; 6/6 probes | Prefix and syntax warning gone; 6/6 probes |
| BCGM25 45 | Extraneous 58-character sentence and duplicated probability block | Prefix gone, **duplicated probability block remains**; 4/4 probes | Extra block absent; 4/4 probes |
| BCIKS20 57 | Repetitive 495-character Greek-letter prefix | No prefix; 6/6 probes | No prefix; 6/6 probes |
| CS25 21 | Content present; 9-character extraneous prefix | No prefix; 6/6 probes | No prefix; 6/6 probes |

Several historical corpus failures did not reproduce as long loops when tested
as isolated pages. Their historical mode included multi-page observations and
older recovery recipes. Do not count all twelve as newly rescued long loops.
The 70 probes are coarse source-checked prose anchors, not a mathematical
transcription score or an exhaustive omission audit.

## Residual repetition: BCGM25 page 45

The source has one small opening display, then a four-line probability bound
ending with `<= n_out * epsilon_MCA(gamma)`: five formula regions in total under
this segmentation. Base grounded emits six. After the legitimate sum of
probabilities at `[279,380,720,464]`, it emits a near-copy of that full expression
at `[280,466,412,482]`, then emits the actual final bound at the overlapping
`[280,464,413,480]`. The extra expression is not in the source.

The duplicated expression varies formatting and some symbols. It occurs only
once extra and is interrupted by grounding markup, so neither an exact
35-token n-gram mask nor a detector requiring several suffix cycles reliably
captures it. `structural_loop=false` and `warnings=[]` are false negatives for
this case. This is **in-body block duplication**, distinct from runaway starts.

The detail-grounded candidate has five formula regions and the source's final
scalar bound without the inserted near-copy. Retain both raw candidates; this
observation does not justify blindly deleting similar equations, since real
derivations legitimately repeat large parts of an expression.

Enabling existing embedded/structural guidance on base grounded did not fix this
case: the output is byte-identical, SHA-256
`045ad3fbaaaa7be9177f3c4bc53226563563db28b1e9a7641fb5ef4da43c16f1`.
Native bias was active on 18 steps, with 44 matched occurrences and zero loop
bias steps. The actual repeated math is outside positive prose steering. This
is a measured limitation of the current policy, not evidence that embedded
geometry cannot help a future block-progress policy.

A next experiment should track already-transcribed source regions and test
geometry-aware continuation scoring at block boundaries. Here the near-copy is
assigned to the same tiny region as the final scalar bound, a stronger signal
than text similarity alone. No such new mechanism was implemented in this turn.

## Other checks and caveats

- ABF26 35: all three outputs retain the header and all 13 data rows. The six
  legitimate `2^{-64.00}` entries and subsequent `2^{-63.99}` through
  `2^{-63.49}` entries match the visible table. The cropped variant's malformed
  math is outside that table (math span 58 has unbalanced braces).
- Jo26 10: all three predict a top coordinate of **-371** for the paragraph
  beginning "Thus the MCA contribution". Opening-marker steering does not
  constrain later coordinate syntax or validity.
- GaoKL24 14: cropped output covers the source's six displays, but the long
  probability derivation still contains incorrect symbols/indices. Passing
  brace/environment checks is not proof of faithful math.
- Neither method was tested here on blank pages or true multi-page decoding.
  The option remains off by default and excluded from multi-page starts.
- Source inspection followed the PDF skill: visually checked complete relevant
  pages and the residual duplicated region; native agreement remained a
  diagnostic, not ground truth. No theorem/proof validation is claimed.

## Reproduce

```sh
HF_HUB_OFFLINE=1 PYTHONPATH=pages2md/src python pages2md/scripts/benchmark_decoding.py \
  --corpus /Users/remco/Documents/Better.codes \
  --output pages2md/experiments/artifacts/new-expanded-run \
  --cases ABF26-34 ABF26-35 ABF26-45 Jo26-10 Jo26-11 Jo26-14 \
          GaoKL24-5 GaoKL24-14 BCGM25-23 BCGM25-45 BCIKS20-57 CS25-21 \
  --variants base-exact base-grounded detail-grounded --max-tokens 4096
```

Observed artifacts: `artifacts/hard-pages-wave03/`, including source renders,
PDF/image hashes, decoder/harness snapshots, 36 raw outputs and metric records.
They are retained in this worktree but ignored by Git. Source PDFs remain in
`/Users/remco/Documents/Better.codes`. No corpus bundles were rewritten.
The one native-guided follow-up and its provenance are retained separately in
`artifacts/hard-pages-wave03-native-check/`.
