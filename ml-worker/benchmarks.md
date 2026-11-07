# Retrieval runtime measurements

## MacBook: MLX versus MPS

Measured on an Apple Silicon MacBook with macOS 27.2, PyTorch 2.11.0,
MLX 0.32.4, and MLX-LM 0.32.2. Chip and RAM size were not recorded. Both
runtimes used the original pinned BF16 zembed and zerank weights, 16 passages
of 400 tokens, and a 32,768-token batch budget. The formatted inputs contained
411 tokens for embedding and 419 for reranking. Medians exclude model loading
and the initial inference and use two warm repetitions.

| Batch | zembed MLX passages/s | zembed MPS passages/s | zerank MLX passages/s | zerank MPS passages/s |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 3.33 | 2.54 | 3.24 | 2.62 |
| 2 | 3.85 | 2.76 | 3.54 | 2.62 |
| 4 | 3.99 | 2.65 | 3.82 | 2.65 |
| 8 | 4.00 | 2.64 | 3.66 | 2.69 |
| 16 | 4.07 | 2.48 | 3.54 | 2.52 |

Batch 32 was limited to 16 supplied passages and did not test a larger batch.
MLX batch 4 processed the embedding request in 4.006 s and reranking in 4.188 s:
45% and 42% higher throughput than each model's best measured MPS setting.
Embedding gains above batch 4 were small; larger reranking batches were slower.
The worker therefore uses native MLX BF16 with at most four inputs and 2,048
padded tokens per call. Longer individual inputs run alone. Two warm samples
do not establish a precise optimum or tail latency.

MLX batch-4 peak allocator memory was 7.91 GiB for embedding and 7.92 GiB for
reranking. MPS process RSS omits GPU allocations and is not directly comparable.
Warm query embedding was about 59 ms with MLX versus 65 ms with MPS. Each model
was measured separately; the worker keeps both selected models resident, so
allow roughly 15 GiB for weights before inference buffers. Combined memory use
and throughput alongside transcription have not been measured.

Relative to each runtime's single-item inference, minimum embedding cosine
agreement was 0.999901 for MLX and 0.999887 for MPS. Reranking order was unchanged,
with maximum raw-score drift of 0.0625 for MLX and 0.125 for MPS. The inputs
contain one relevant passage and repeated distractors. These checks do not
establish cross-runtime agreement or retrieval quality.

## Reproduce on the MacBook

The benchmark uses the production loader, formatting, pooling, and scoring.
Weights and revisions are pinned in `src/ml_worker/text.py`; neither loading
nor inference executes model-repository Python code or quantizes the weights.

```sh
nix develop .#ml-worker --command bash
uv sync --project ml-worker --extra dev
for model in embed rerank; do
  PYTHONPATH=ml-worker/src uv run --project ml-worker \
    python ml-worker/benchmark.py "$model" \
    --tokens 400 --candidates 30 --repeats 5 --compare-single
done
```

Each process loads one model and excludes loading and the first inference from
warm timing. `--batch-size` and `--batch-tokens` allow further sweeps; the worker
uses fixed limits. `--compare-single` checks against that runtime's single-item
inference. Peak RSS and MLX allocator peaks are cumulative within the process;
run each workload separately when comparing memory. Test mixed passage lengths
and full queued searches to assess production latency.

## CPU support decision

Historical measurements on Zawrat's Intel Core i5-14500, using PyTorch 2.11.0,
Transformers 5.12.1, FP32, and eight threads:

| Operation | Warm median | Peak process RSS |
| --- | ---: | ---: |
| Embed short query | 0.625 s | 15.46 GiB |
| Embed one 128-token passage | 3.61 s | 15.46 GiB |
| Embed one 512-token passage | 11.35 s | 15.51 GiB |
| Rerank 30 × 128-token passages sequentially | 104.12 s | 15.44 GiB |

Those throughput and memory costs are unsuitable for this deployment. The
worker no longer provides CPU text inference; Linux audio recipes remain.
The full historical CPU investigation is in commit `8b3a406`. A later batch-4
FP32 attempt stalled during loading before a session restart and produced no
warm samples, so it supplies no additional throughput evidence.

## Search deadline

The default `provider.request_timeout_seconds` is 30 seconds per inference stage.
Thirty 400-token reranking candidates are estimated at 7.9 seconds warm from
3.82 passages/s; this extrapolates from the measured 16-passage requests. Allow
5 seconds for startup/loading, 15 seconds for queueing, and 2 seconds for polling
and network overhead, then round to 30 seconds. Selected text models are loaded
before the worker claims its first job and remain resident between jobs.

Embedding and reranking run sequentially, so search can wait roughly 60 seconds
in total. The WebUI lets the daemon enforce these deadlines. Longer passages,
more candidates, or sustained queue contention may require a higher configured
timeout; the small benchmark sample does not establish p95/p99 latency. Worker
leases and large-file transport timeouts are separate from the search budget.
