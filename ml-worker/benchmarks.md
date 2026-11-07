# Retrieval runtime measurements

CPU inference has been removed. The CPU results and commands below describe the
historical harness at commit `8b3a406`; the current worker and benchmark use
MLX only on Apple Silicon. MacBook measurements appear after the CPU history.

## Historical ZE on Zawrat CPU

Measured on 2026-11-06 in Zawrat's Codex container, on its actual Intel
Core i5-14500 (6 performance cores, 8 efficiency cores, 20 hardware threads).
The host has 46.8 GiB RAM and runs other services. It supports AVX2/AVX-VNNI,
but has no native BF16 instructions. These are PyTorch CPU measurements, not
a claim about the models' best possible performance with other inference engines.
The container has a 16 GiB soft memory limit and 24 GiB hard limit. FP32 approaches
the soft limit; telemetry showed no swap use or OOM events during the run.

Models are the worker's pinned
[`zembed-1`](https://huggingface.co/zeroentropy/zembed-1-embedding/tree/cf13c81f3274394053d166740294f7eea4586f7a)
and
[`zerank-2`](https://huggingface.co/zeroentropy/zerank-2-reranker/tree/5eae30d5ee3c6b2df2ef6d723bde45172d761c4c).
Runtime: Python 3.14.2, PyTorch 2.11.0+cpu, Transformers 5.12.1, Linux 6.18.59.
Each process runs one model, one input at a time, using the production prompt,
pooling and scoring code. Models stay loaded during timing. Downloads and model
initialization are excluded from inference times. Initialization uses cached
weights; it is not a cold-boot disk benchmark.

## Results

The original BF16 implementation, limited to four threads:

| Operation | Warm median | Warm samples | Peak process RSS |
| --- | ---: | --- | ---: |
| Embed short query (17 formatted tokens) | 2.10 s | 2.098, 2.096 s | 7.20 GiB |
| Embed one 128-token passage (139 formatted tokens) | 14.96 s | 14.078, 15.835 s | 7.20 GiB |
| Rerank one 128-token passage (147 formatted tokens) | 18.46 s | 19.810, 17.112 s | 7.90 GiB |

Switching the same reranker weights to FP32 at **four threads** took 3.72 s
(3.833, 3.602 s), with 15.44 GiB peak RSS. Its relevant-passage score was
12.4059 versus BF16's 12.4375. The former CPU backend therefore used FP32. Apple Silicon uses native MLX
BF16 (see the MacBook measurements below). This is a precision change, without changing weights or introducing a
quantized recipe. BF16/FP32 embedding cosine agreement was 0.999921 for the
query and 0.999939 for the 128-token passage.

FP32 with eight threads:

| Operation | Warm median | Warm samples | Peak process RSS |
| --- | ---: | --- | ---: |
| Embed short query (17 formatted tokens) | 0.625 s | 0.624, 0.626 s | 15.46 GiB |
| Embed 1 × 128-token passage | 3.61 s | 3.608, 3.610 s | 15.46 GiB |
| Embed 1 × 512-token passage | 11.35 s | 11.422, 11.275 s | 15.51 GiB |
| Rerank 1 × 128-token passage | 3.65 s | 3.452, 3.856 s | 15.44 GiB |
| Rerank 10 × 128-token passages | 37.57 s | 38.744, 36.390 s | 15.44 GiB |
| Rerank 30 × 128-token passages | 104.12 s | 104.615, 103.635 s | 15.44 GiB |
| Rerank 1 × 512-token passage | 14.83 s | 11.623, 18.033 s | 15.50 GiB |

Reranker initialization from cached weights took 3.08 s, and its first single-
passage inference took 3.69 s. The first 10- and 30-candidate requests took
36.55 s and 105.66 s. Both candidate-count results above are measured full
requests, with model loading excluded.

Embedding initialization took 3.27 s; first inference took 0.643 s for the
query, 3.586 s for 128 tokens and 11.807 s for 512 tokens. Embedding inputs
contain 11 additional template tokens, and query/passage scoring adds 19.

The eight-thread FP32 rerank uses synthetic passages of the stated token lengths,
with one relevant release decision and storage, audio and monitoring distractors.
The relevant passage must rank first. The 512-token case varied substantially
between its two warm runs; its first inference took 11.65 s. This is a latency benchmark
and a semantic smoke check, not a retrieval-quality evaluation.

Query embedding is plausible for local retrieval with a resident model, but this
reranker implementation is too slow for interactive search. Mdstore defaults to
30 candidates and approximately 400-token chunks, so the 128-token benchmark
is an optimistic workload. Two resident FP32 model processes would require
roughly 30 GiB based on the individual measurements; co-residency was not tested.
These measurements motivate an optimized, quality-checked inference backend.
At the time of these CPU measurements, mdstore queued inference with a
300-second job lifetime. Queue integration did not improve model latency. The
current MLX search budget is documented below.

The Nix-packaged Python 3.13 runtime was also checked with a real query through
the production CPU loader: 0.609 s warm median (0.596, 0.612, 0.609 s), with
0.999921 cosine agreement against BF16. Loading and computing the first query
took 6.0–9.0 s in two separate processes, excluding Python/Torch import startup.
The queued worker currently reloads the model for every job; warm latency is
what a future persistent retrieval service could target.

## Reproduce

```sh
nix develop .#ml-worker --command bash --noprofile --norc
uv sync --frozen --project ml-worker
OMP_NUM_THREADS=8 uv run --frozen --project ml-worker python ml-worker/benchmark.py \
  rerank --precision fp32 --threads 8 --tokens 128 --candidates 1 10 30 --repeats 2
OMP_NUM_THREADS=8 uv run --frozen --project ml-worker python ml-worker/benchmark.py \
  embed --precision fp32 --threads 8 --tokens 128 512 --repeats 2
OMP_NUM_THREADS=8 uv run --frozen --project ml-worker python ml-worker/benchmark.py \
  rerank --precision fp32 --threads 8 --tokens 512 --candidates 1 --repeats 2
```

Use `--precision bf16 --threads 4` to measure the original precision. The first
iteration of every case is reported separately and excluded from the warm
median. `--candidates` controls actual full rerank requests, not multiplication
of a single-passage time. RSS is the process high-water mark, including loading.
Other services remain running and CPU affinity is unrestricted, so short runs
vary. This does not measure concurrent requests or both models resident together.

## INT8 experiment

PyTorch dynamic INT8 linear layers reduced the short-query median to 0.198 s
and the 128-token embedding median to 2.71 s at four threads. However, the
resulting embeddings had cosine agreement of only **0.803** (query) and **0.844**
(passage) with the BF16 baseline. The reranker's relevant-passage score changed
from 12.4375 to 0.0634. Changed raw scores alone do not establish ranking-quality
loss, and the embedding agreement failure does not establish reranker quality.
A follow-up three-query smoke check still put the relevant document first in all
three cases for both FP32 and INT8. No retrieval-quality evaluation was performed;
quantization is not enabled in production.

The original INT8 reranker median was **3.14 s per 128-token passage** at four
threads (2.877, 3.136, 3.548 s), versus FP32's 3.72 s: a 1.19× speedup. A separate
follow-up run measured 2.18 s INT8 versus 3.33 s FP32, both at four threads, but
with only one warm sample each. This illustrates shared-host variability. Full
10- and 30-candidate INT8 requests have not been measured. The follow-up INT8
process used 7.25 GiB RSS after inference, versus FP32's 15.38 GiB; converting
weights at startup peaked at 15.44 GiB.

The experiment loaded the pinned BF16 weights, replaced each `torch.nn.Linear`
with `torch.ao.nn.quantized.dynamic.Linear.from_float(layer.float())` using
`torch.ao.quantization.default_dynamic_qconfig`, then converted remaining
nonquantized parameters to FP32. Initialization took 27.5 s for zembed and
33.6 s for zerank, with 14.4 and 13.6 GiB peak RSS respectively. This experimental
quantization is not part of the worker.

## CPU kernel profile

The installed PyTorch build includes MKL 2024.2, oneDNN 3.10.2, OpenMP and
FBGEMM. Its ATen CPU capability is AVX2; the quantization engine is `x86`.
`MKL_VERBOSE=1` confirms AVX2 dispatch and `SGEMM` calls with four threads.
Profiling one 128-token passage at four threads shows:

| Precision | Dominant operator | Share of self CPU operator time | CPU flash attention |
| --- | --- | ---: | ---: |
| FP32 | `aten::mm` | 95.04% | 1.79% |
| Dynamic INT8 | `quantized::linear_dynamic` | 92.49% | 3.30% |

These profiles use optimized native matrix kernels, with the model in eager
mode. Candidates are processed sequentially. No `torch.compile`, calibrated
quantization or alternative inference engine was tested. Removing Python
overhead alone cannot address a matrix-dominated workload. The dynamic INT8
operator also quantizes activations and returns FP32 between linear layers;
see the [PyTorch 2.11 implementation](https://github.com/pytorch/pytorch/blob/v2.11.0/aten/src/ATen/native/quantized/cpu/qlinear_dynamic.cpp).
This is not a fully integer transformer graph.

## MacBook MLX versus MPS

Measured on the user's Apple Silicon MacBook with macOS 27.2, PyTorch 2.11.0,
MLX 0.32.4, and MLX-LM 0.32.2. The chip and RAM size were not supplied. Both
runtimes used the pinned original BF16 weights, 16 passages of 400 tokens
(411 formatted for embedding, 419 for reranking), a 32,768-token batch budget,
one initial inference and two warm repetitions. Loading and the initial
inference are excluded from the medians. These synthetic inputs contain one
relevant passage and repeated distractors; they are not a quality evaluation.

| Batch | zembed MLX passages/s | zembed MPS passages/s | zerank MLX passages/s | zerank MPS passages/s |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 3.33 | 2.54 | 3.24 | 2.62 |
| 2 | 3.85 | 2.76 | 3.54 | 2.62 |
| 4 | 3.99 | 2.65 | 3.82 | 2.65 |
| 8 | 4.00 | 2.64 | 3.66 | 2.69 |
| 16 | 4.07 | 2.48 | 3.54 | 2.52 |

Batch 32 was limited to 16 supplied passages and does not test a larger batch.
MLX batch 4 processed the complete embedding request in 4.006 s and reranking
in 4.188 s. Compared with each model's best measured MPS setting, throughput
improved by 45% and 42% respectively. MLX embedding gains above batch 4 were
small; larger reranking batches were slower. Two warm samples do not establish
a precise optimum. The worker requires MLX on Apple Silicon and fixes the
inference batch limit at 4, retaining the 2,048-token memory guard.

At MLX batch 4, peak MLX allocator memory was 7.91 GiB for embedding and
7.92 GiB for reranking. MPS process RSS omits GPU allocations and cannot be
compared directly with this allocator measurement. Warm query embedding was
about 59 ms with MLX versus 65 ms with MPS. The two models were measured in
separate processes; co-residency was not measured.

Relative to each runtime's own single-item inference, MLX embedding minimum
cosine agreement was 0.999901 (MPS: 0.999887). Reranking order was identical on
these inputs, with maximum raw-score drift 0.0625 for MLX and 0.125 for MPS.
These checks do not establish cross-runtime agreement or retrieval quality.

The benchmark now uses the production loader and inference code. It loads
original pinned safetensors, disables remote Python code, and does not quantize
or substitute a community checkpoint. Text inference depends on MLX-LM; PyTorch and CPU text support were removed. To check the selected settings from the MacBook repository:

```sh
nix develop .#ml-worker --command bash
uv sync --project ml-worker --extra dev
for model in embed rerank; do
  PYTHONPATH=ml-worker/src uv run --project ml-worker \
    python ml-worker/benchmark.py "$model" \
    --tokens 400 --candidates 30 --repeats 5 --compare-single
done
```

Each run loads one model and excludes loading and the first inference from warm
timing. The benchmark retains batch controls for experiments, but the worker
uses a fixed maximum of four. Test mixed passage lengths and full queued
searches before assuming the synthetic warm timings represent production
latency: model loading, queueing, and long document jobs still affect search.


## CPU support decision

The earlier i5-14500 measurements show a 16 GiB process footprint for FP32,
11.35 s to embed one 512-token passage, and 104.12 s to rerank 30 shorter
128-token passages sequentially. These costs are unsuitable for this deployment
relative to the MacBook's roughly four passages per second at 400 tokens.

A fresh eight-thread FP32, batch-4 run was attempted on the same i5-14500 after
the MacBook sweep. The final attempt reached 346/398 loading operations after
7 minutes 45 seconds, then the session restarted. It produced no warm inference
samples. This is an incomplete benchmark, not a new throughput measurement;
the restart's cause was not established. The earlier measured throughput and
memory cost are sufficient to reject this backend for this deployment.

The worker no longer installs PyTorch, offers CPU text inference, or advertises
text recipes on Linux. Linux audio recipes remain available. This decision does
not claim that all CPUs or alternative inference engines have the same limits.


## Search deadline

The default `provider.request_timeout_seconds` is 30 seconds per inference stage.
The default 30-candidate, 400-token rerank is estimated at 7.9 seconds warm from
3.82 passages/s; this is an extrapolation from the measured 16-passage requests.
Allow 5 seconds for process startup and model loading, 15 seconds for queueing,
and 2 seconds for polling and network overhead, then round to 30 seconds.
The stage deadline includes queueing, not just model execution. Embedding and
reranking may therefore wait up to roughly 60 seconds together. Clients must
allow both stages; the WebUI lets the daemon enforce these deadlines.

The small benchmark sample does not establish p95/p99 latency. Longer passages,
more candidates, concurrent foreground requests, or long document jobs may need
a higher configured timeout. Timeout returns degraded search results rather
than holding callers indefinitely. Worker lease renewal and large-file transport
timeouts are separate from this search budget.
