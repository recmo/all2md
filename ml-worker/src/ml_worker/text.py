"""Pinned ZeroEntropy retrieval models, without executing repository code."""
from __future__ import annotations

import math

EMBED_MODEL = "zeroentropy/zembed-1-embedding"
EMBED_REVISION = "cf13c81f3274394053d166740294f7eea4586f7a"
RERANK_MODEL = "zeroentropy/zerank-2-reranker"
RERANK_REVISION = "5eae30d5ee3c6b2df2ef6d723bde45172d761c4c"
EMBED_RECIPE = "text-embeddings-zembed-1-v1"
RERANK_RECIPE = "text-rerank-zerank-2-v1"
DIMENSIONS = 2560
MAX_TOKENS = 32768
MAX_ITEMS = 256
MAX_TEXT = 32768
INFERENCE_BATCH_SIZE = 8
INFERENCE_BATCH_TOKENS = 2048
# Pinned zerank-2 1_LogitScore/config.json selects the raw "Yes" logit.
YES_TOKEN = 9454


def records(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ITEMS:
        raise ValueError(f"texts must contain 1..{MAX_ITEMS} records")
    ids = set()
    for record in value:
        if not isinstance(record, dict) or set(record) != {"id", "text"}:
            raise ValueError("each text requires exactly id and text")
        identity, text = record["id"], record["text"]
        if not isinstance(identity, str) or not identity.strip() or identity in ids:
            raise ValueError("text IDs must be nonempty and unique")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError("text must be nonempty and at most 32768 characters")
        ids.add(identity)
    return value


def _load(model_id, revision, *, classifier, device):
    import torch
    from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer
    if device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(model_id, revision=revision, trust_remote_code=False)
    factory = AutoModelForCausalLM if classifier else AutoModel
    model = factory.from_pretrained(model_id, revision=revision, trust_remote_code=False,
                                   use_safetensors=True,
                                   dtype=torch.float32 if device == "cpu" else torch.bfloat16).to(device).eval()
    return tokenizer, model, device


def _batches(tokenizer, formatted, device):
    # Left padding keeps the real final token at -1 for pooling and scoring.
    tokenizer.padding_side = "left"
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    # Reject oversize inputs rather than losing text or the closing scoring prompt.
    encoded = tokenizer(formatted, truncation=False, padding=False)
    lengths = [len(ids) for ids in encoded["input_ids"]]
    if max(lengths) > MAX_TOKENS:
        raise ValueError(f"formatted input exceeds {MAX_TOKENS} tokens; split into smaller passages")
    start = 0
    while start < len(lengths):
        end = start + 1
        longest = lengths[start]
        while end < len(lengths) and end - start < INFERENCE_BATCH_SIZE:
            longest = max(longest, lengths[end])
            if longest * (end - start + 1) > INFERENCE_BATCH_TOKENS:
                break
            end += 1
        # An input larger than the batch budget runs alone, within MAX_TOKENS.
        yield tokenizer.pad({key: value[start:end] for key, value in encoded.items()},
                            padding=True, return_tensors="pt").to(device)
        start = end


def embeddings(parameters, *, device="auto"):
    import torch
    items = records(parameters.get("texts"))
    input_type = parameters.get("input_type", "document")
    if input_type not in {"query", "document"}:
        raise ValueError("input_type must be query or document")
    namespace = parameters.get("namespace", "documents")
    if not isinstance(namespace, str) or not namespace.strip() or namespace == "speakers":
        raise ValueError("a non-speaker text namespace is required")
    tokenizer, model, device = _load(EMBED_MODEL, EMBED_REVISION, classifier=False, device=device)
    vectors = []
    # config_sentence_transformers.json + modeling_zembed.py add this prefix/suffix.
    formatted = [f"<|im_start|>system\n{input_type}<|im_end|>\n<|im_start|>user\n{item['text']}<|im_end|>\n"
                 for item in items]
    for batch in _batches(tokenizer, formatted, device):
        with torch.inference_mode():
            # 1_Pooling/config.json specifies last-token pooling, including the prompt.
            hidden = model(**batch, use_cache=False).last_hidden_state[:, -1].float()
            vectors.extend(torch.nn.functional.normalize(hidden, p=2, dim=1).cpu().tolist())
    if len(vectors) != len(items) or any(
        len(v) != DIMENSIONS or not all(math.isfinite(x) for x in v)
        or not 0.99 <= sum(x*x for x in v) <= 1.01 for v in vectors
    ):
        raise ValueError("model returned invalid embeddings")
    return {
        "space": {"namespace": namespace, "recipe": EMBED_RECIPE, "dimensions": DIMENSIONS},
        "model": EMBED_MODEL, "revision": EMBED_REVISION, "max_tokens": MAX_TOKENS,
        "records": [{"id": item["id"], "vector": vector,
                     "metadata": {"text": item["text"], "input_type": input_type}}
                    for item, vector in zip(items, vectors, strict=True)],
    }


def rerank(parameters, *, device="auto"):
    import torch
    items = records(parameters.get("texts"))
    query = parameters.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_TEXT:
        raise ValueError("query must be nonempty and at most 32768 characters")
    top_n = parameters.get("top_n", len(items))
    if type(top_n) is not int or not 1 <= top_n <= len(items):
        raise ValueError("top_n must be between 1 and the number of texts")
    tokenizer, model, device = _load(RERANK_MODEL, RERANK_REVISION, classifier=True, device=device)
    scores = []
    # Pinned chat_template.jinja: query is system, document is user; score next token.
    formatted = [tokenizer.apply_chat_template(
        [{"role": "query", "content": query}, {"role": "document", "content": item["text"]}],
        tokenize=False, add_generation_prompt=True) for item in items]
    for batch in _batches(tokenizer, formatted, device):
        with torch.inference_mode():
            logits = model(**batch, use_cache=False, logits_to_keep=1).logits
            scores.extend(logits[:, -1, YES_TOKEN].float().cpu().tolist())
    if len(scores) != len(items) or not all(math.isfinite(x) for x in scores):
        raise ValueError("model returned invalid reranking scores")
    order = sorted(range(len(items)), key=lambda i: (-scores[i], i))[:top_n]
    return {"model": RERANK_MODEL, "revision": RERANK_REVISION, "max_tokens": MAX_TOKENS,
            "query": query, "results": [{"index": i, "id": items[i]["id"],
                                        "relevance_score": scores[i]} for i in order]}
