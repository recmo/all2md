"""Pinned encoder models, using their documented pooling and input conventions."""
from __future__ import annotations

import math

EMBED_MODEL = "intfloat/multilingual-e5-small"
EMBED_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
RERANK_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
RERANK_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
EMBED_RECIPE = "text-embeddings-e5-small-v1"
RERANK_RECIPE = "text-rerank-minilm-v1"
DIMENSIONS = 384
MAX_ITEMS = 256
MAX_TEXT = 32768


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
    from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
    if device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(model_id, revision=revision, trust_remote_code=False)
    factory = AutoModelForSequenceClassification if classifier else AutoModel
    model = factory.from_pretrained(model_id, revision=revision, trust_remote_code=False,
                                   use_safetensors=True).to(device).eval()
    return tokenizer, model, device


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
    prefix = "query: " if input_type == "query" else "passage: "
    for start in range(0, len(items), 16):
        batch = tokenizer([prefix + item["text"] for item in items[start:start + 16]],
                          max_length=512, padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.inference_mode():
            hidden = model(**batch).last_hidden_state
            mask = batch["attention_mask"][..., None]
            pooled = hidden.masked_fill(~mask.bool(), 0).sum(1) / mask.sum(1).clamp(min=1)
            vectors.extend(torch.nn.functional.normalize(pooled, p=2, dim=1).cpu().tolist())
    if len(vectors) != len(items) or any(
        len(v) != DIMENSIONS or not all(math.isfinite(x) for x in v)
        or not 0.99 <= sum(x*x for x in v) <= 1.01 for v in vectors
    ):
        raise ValueError("model returned invalid embeddings")
    return {
        "space": {"namespace": namespace, "recipe": EMBED_RECIPE, "dimensions": DIMENSIONS},
        "model": EMBED_MODEL, "revision": EMBED_REVISION, "max_tokens": 512,
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
    for start in range(0, len(items), 16):
        texts = [item["text"] for item in items[start:start + 16]]
        batch = tokenizer([query]*len(texts), texts, max_length=512,
                          padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.inference_mode():
            scores.extend(model(**batch).logits.reshape(-1).cpu().tolist())
    if len(scores) != len(items) or not all(math.isfinite(x) for x in scores):
        raise ValueError("model returned invalid reranking scores")
    order = sorted(range(len(items)), key=lambda i: (-scores[i], i))[:top_n]
    return {"model": RERANK_MODEL, "revision": RERANK_REVISION, "max_tokens": 512,
            "query": query, "results": [{"index": i, "id": items[i]["id"],
                                        "relevance_score": scores[i]} for i in order]}
