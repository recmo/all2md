"""Native MLX benchmark path for the pinned, unquantized Qwen3 retrieval weights."""
import mlx.core as mx
from huggingface_hub import snapshot_download
from mlx_lm import load as load_model

from ml_worker import text


def load(model_id, revision, precision):
    if not mx.metal.is_available():
        raise RuntimeError("MLX benchmark requires Apple Silicon with Metal")
    path = snapshot_download(model_id, revision=revision,
                             allow_patterns=["*.json", "*.safetensors", "*.txt", "*.jinja", "*.model"])
    model, _ = load_model(path, tokenizer_config={"trust_remote_code": False})
    if not model.args.tie_word_embeddings:
        raise ValueError("benchmark expects the pinned models' tied readout weights")
    model.apply(lambda value: value.astype(mx.bfloat16 if precision == "bf16" else mx.float32))
    model.eval()
    mx.eval(model.parameters())
    return model


def inference(parameters, *, model, tokenizer, embedding):
    items = text.records(parameters["texts"])
    if embedding:
        mode = parameters["input_type"]
        formatted = [f"<|im_start|>system\n{mode}<|im_end|>\n<|im_start|>user\n{item['text']}<|im_end|>\n"
                     for item in items]
    else:
        formatted = [tokenizer.apply_chat_template(
            [{"role": "query", "content": parameters["query"]},
             {"role": "document", "content": item["text"]}],
            tokenize=False, add_generation_prompt=True) for item in items]
    encoded = tokenizer(formatted, truncation=False, padding=False)["input_ids"]
    if max(map(len, encoded)) > text.MAX_TOKENS:
        raise ValueError("formatted input exceeds model context")
    values = []
    start = 0
    while start < len(encoded):
        end = start + 1
        width = len(encoded[start])
        while end < len(encoded) and end - start < text.INFERENCE_BATCH_SIZE:
            longest = max(width, len(encoded[end]))
            if longest * (end - start + 1) > text.INFERENCE_BATCH_TOKENS:
                break
            width = longest
            end += 1
        rows = encoded[start:end]
        # Qwen3 is causal: right-side pads cannot affect a preceding real token.
        pad = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
        inputs = mx.array([row + [pad] * (width - len(row)) for row in rows])
        hidden = model.model(inputs)
        last = hidden[mx.arange(len(rows)), mx.array([len(row) - 1 for row in rows])]
        if embedding:
            output = last.astype(mx.float32)
            output = output / mx.linalg.norm(output, axis=1, keepdims=True)
        else:
            # Both pinned models tie the token embedding and readout weights.
            output = model.model.embed_tokens.as_linear(last)[:, text.YES_TOKEN].astype(mx.float32)
        mx.eval(output)
        values.extend(output.tolist())
        start = end
    if embedding:
        return {"records": [{"id": item["id"], "vector": vector}
                            for item, vector in zip(items, values, strict=True)]}
    order = sorted(range(len(items)), key=lambda i: (-values[i], i))
    return {"results": [{"id": items[i]["id"], "index": i, "relevance_score": values[i]}
                        for i in order]}
