"""Measure the pinned ZE models on the production native MLX backend."""
import argparse
import importlib.metadata
import json
import platform
import resource
import statistics
import sys
import time
from unittest.mock import patch

import numpy as np

from ml_worker import text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=["embed", "rerank"])
    parser.add_argument("--precision", choices=["bf16", "fp32"], default="bf16")
    parser.add_argument("--tokens", type=int, nargs="+", default=[128])
    parser.add_argument("--candidates", type=int, nargs="+", default=[1])
    parser.add_argument("--batch-size", type=int, default=text.INFERENCE_BATCH_SIZE)
    parser.add_argument("--batch-tokens", type=int, default=text.INFERENCE_BATCH_TOKENS)
    parser.add_argument("--compare-single", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if (args.repeats < 1 or min(args.tokens) < 1
            or args.batch_size < 1 or args.batch_tokens < 1
            or not all(1 <= n <= text.MAX_ITEMS for n in args.candidates)):
        parser.error("repeats, batch limits and tokens must be positive; candidates must be 1..256")
    text.INFERENCE_BATCH_SIZE = args.batch_size
    text.INFERENCE_BATCH_TOKENS = args.batch_tokens
    embedding = args.model == "embed"
    model_id, revision = ((text.EMBED_MODEL, text.EMBED_REVISION) if embedding
                          else (text.RERANK_MODEL, text.RERANK_REVISION))
    start = time.perf_counter()
    tokenizer, model = text._load(model_id, revision, precision=args.precision)
    load_seconds = time.perf_counter() - start
    print(json.dumps({"model": model_id, "revision": revision, "precision": args.precision,
                      "device": "mlx", "batch_size": args.batch_size,
                      "batch_tokens": args.batch_tokens,
                      "mlx": importlib.metadata.version("mlx"),
                      "mlx_lm": importlib.metadata.version("mlx-lm"),
                      "platform": platform.platform(), "load_seconds": load_seconds}), flush=True)
    query = "When is the release shipping?"
    passages = [
        "We agreed to ship the release on Friday. The team reviewed the deployment plan "
        "and assigned ownership of the remaining release tasks.",
        "Assets are stored by their content hash. The storage service validates uploads "
        "and publishes document changes atomically after checking their dependencies.",
        "Meeting Capture records microphone and participant audio separately. The "
        "processing worker adjusts playback levels and suppresses background noise.",
        "The monitoring dashboard shows CPU utilization, available memory and service "
        "health. Alerts identify failed probes and stale telemetry collectors.",
    ]

    def passage(index, tokens):
        sentence = passages[0 if index == 0 else 1 + (index - 1) % 3]
        return tokenizer.decode(tokenizer.encode((sentence + " ") * tokens,
                                                 add_special_tokens=False)[:tokens])

    # Exercise the production formatting, pooling and scoring, while keeping each
    # model resident. The queued worker currently reloads it for every job.
    with patch.object(text, "_load", return_value=(tokenizer, model)):
        for tokens in ([0] + args.tokens if embedding else args.tokens):
            for count in ([1] if tokens == 0 else args.candidates):
                items = [{"id": str(i), "text": query if tokens == 0 else passage(i, tokens)}
                         for i in range(count)]
                input_type = "query" if tokens == 0 else "document"
                parameters = {"texts": items, "input_type": input_type, "query": query}
                inference = text.embeddings if embedding else text.rerank
                times = []
                for _ in range(args.repeats + 1):
                    start = time.perf_counter()
                    result = inference(parameters)
                    times.append(time.perf_counter() - start)
                if embedding:
                    formatted = (f"<|im_start|>system\n{input_type}<|im_end|>\n"
                                 f"<|im_start|>user\n{items[0]['text']}<|im_end|>\n")
                else:
                    formatted = tokenizer.apply_chat_template(
                        [{"role": "query", "content": query},
                         {"role": "document", "content": items[0]["text"]}],
                        tokenize=False, add_generation_prompt=True)
                    assert result["results"][0]["id"] == "0", "relevant passage lost first place"
                peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                memory = {}
                import mlx.core as mx
                memory["mlx_peak_allocated_gib"] = mx.get_peak_memory() / 1024**3
                comparison = {}
                if args.compare_single:
                    with patch.object(text, "INFERENCE_BATCH_SIZE", 1):
                        baseline = inference(parameters)
                    if embedding:
                        vectors = np.array([r["vector"] for r in result["records"]])
                        reference = np.array([r["vector"] for r in baseline["records"]])
                        comparison["min_cosine_to_single"] = float(
                            (np.sum(vectors * reference, axis=1) /
                             (np.linalg.norm(vectors, axis=1) * np.linalg.norm(reference, axis=1))).min())
                    else:
                        reference = {r["id"]: r["relevance_score"] for r in baseline["results"]}
                        comparison["max_score_delta_to_single"] = max(
                            abs(r["relevance_score"] - reference[r["id"]]) for r in result["results"])
                        comparison["same_ranking_as_single"] = (
                            [r["id"] for r in result["results"]] == [r["id"] for r in baseline["results"]])
                print(json.dumps({
                    "input_type": input_type if embedding else "query/document",
                    "passage_tokens": tokens, "candidates": count,
                    "formatted_tokens": len(tokenizer.encode(formatted)),
                    "first_seconds": times[0], "warm_seconds": times[1:],
                    "median_seconds": statistics.median(times[1:]),
                    "max_warm_seconds": max(times[1:]),
                    "passages_per_second": count / statistics.median(times[1:]),
                    "peak_rss_gib": peak_rss / (1024**3 if sys.platform == "darwin" else 1024**2),
                    **comparison,
                    **memory,
                }), flush=True)


if __name__ == "__main__":
    main()
