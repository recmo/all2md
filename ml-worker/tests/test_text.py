"""Exercise native MLX loading, batching, and the published retrieval artifacts."""
import json
import math

import pytest
from ml_worker import text


def test_native_mlx_loading_and_retrieval_artifacts(tmp_path, monkeypatch):
    mx = pytest.importorskip('mlx.core')
    pytest.importorskip('mlx_lm')
    from mlx_lm.models.qwen3 import Model, ModelArgs
    from mlx.utils import tree_flatten
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import AutoTokenizer, PreTrainedTokenizerFast
    import huggingface_hub

    mx.random.seed(7)
    config = dict(model_type='qwen3', vocab_size=text.YES_TOKEN + 1, hidden_size=32,
                  intermediate_size=64, num_hidden_layers=2, num_attention_heads=4,
                  num_key_value_heads=2, head_dim=8, tie_word_embeddings=True,
                  rms_norm_eps=1e-6, max_position_embeddings=32768, rope_theta=10000)
    model = Model(ModelArgs.from_dict(config))
    mx.save_safetensors(str(tmp_path / 'model.safetensors'), dict(tree_flatten(model.parameters())))
    (tmp_path / 'config.json').write_text(json.dumps(config))
    backend = Tokenizer(models.WordLevel(
        {'[UNK]': 0, '[PAD]': 1, '[EOS]': 2, 'alpha': 3, 'beta': 4}, unk_token='[UNK]'))
    backend.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token='[UNK]',
                                       pad_token='[PAD]', eos_token='[EOS]')
    tokenizer.chat_template = "{% for message in messages %}{{message['content']}} {% endfor %}alpha"
    tokenizer.save_pretrained(tmp_path)
    monkeypatch.setattr(text, 'DIMENSIONS', 32)
    # The test uses MLX's CPU implementation; production requires Metal.
    monkeypatch.setattr(mx.metal, 'is_available', lambda: True)
    monkeypatch.setattr(AutoTokenizer, 'from_pretrained', lambda *args, **kwargs: tokenizer)
    downloads = []
    def snapshot(model_id, *, revision, allow_patterns):
        downloads.append((model_id, revision))
        assert '*.safetensors' in allow_patterns
        return str(tmp_path)
    monkeypatch.setattr(huggingface_hub, 'snapshot_download', snapshot)
    original_load = text._load
    loaded = {identity: original_load(identity, revision, precision='fp32') for identity, revision in
              [(text.EMBED_MODEL, text.EMBED_REVISION), (text.RERANK_MODEL, text.RERANK_REVISION)]}
    assert downloads == [(text.EMBED_MODEL, text.EMBED_REVISION),
                         (text.RERANK_MODEL, text.RERANK_REVISION)]
    # Check the production BF16 default separately from FP32 batch comparisons.
    _, bf16 = original_load(text.EMBED_MODEL, text.EMBED_REVISION)
    assert all(value.dtype == mx.bfloat16 for _, value in tree_flatten(bf16.parameters()))
    monkeypatch.setattr(text, '_load', lambda identity, revision: loaded[identity])
    items = [{'id': str(i), 'text': 'alpha ' * (i + 1) + 'beta'} for i in range(30)]
    parameters = {'texts': items, 'input_type': 'document', 'namespace': 'search',
                  'query': 'alpha beta', 'top_n': 10}
    vectors = text.embeddings(parameters)
    scores = text.rerank(parameters)
    assert vectors['space'] == {'namespace': 'search', 'recipe': text.EMBED_RECIPE, 'dimensions': 32}
    assert vectors['revision'] == text.EMBED_REVISION
    assert scores['revision'] == text.RERANK_REVISION
    assert [r['id'] for r in vectors['records']] == [r['id'] for r in items]
    for item, record in zip(items, vectors['records'], strict=True):
        assert record['metadata'] == {'text': item['text'], 'input_type': 'document'}
        assert abs(sum(x*x for x in record['vector']) - 1) < 1e-5
    assert len(scores['results']) == 10
    assert len({r['index'] for r in scores['results']}) == 10
    for result in scores['results']:
        assert result['id'] == items[result['index']]['id']
        assert math.isfinite(result['relevance_score'])
    assert scores['results'] == sorted(scores['results'], key=lambda r: (-r['relevance_score'], r['index']))
    monkeypatch.setattr(text, 'INFERENCE_BATCH_SIZE', 1)
    single_vectors = text.embeddings(parameters)
    single_scores = text.rerank(parameters)
    for a, b in zip(vectors['records'], single_vectors['records'], strict=True):
        assert sum(x*y for x, y in zip(a['vector'], b['vector'], strict=True)) > .99999
    assert [r['index'] for r in scores['results']] == [r['index'] for r in single_scores['results']]
    for a, b in zip(scores['results'], single_scores['results'], strict=True):
        assert abs(a['relevance_score'] - b['relevance_score']) < 1e-5
    monkeypatch.setattr(text, 'INFERENCE_BATCH_SIZE', 4)
    monkeypatch.setattr(text, 'INFERENCE_BATCH_TOKENS', 1)
    assert text.rerank(parameters) == single_scores
    with pytest.raises(ValueError): text.embeddings({**parameters, 'namespace': 'speakers'})
    with pytest.raises(ValueError): text.rerank({**parameters, 'top_n': True})
    with pytest.raises(ValueError): text.records([items[0], items[0]])
    monkeypatch.setattr(text, 'MAX_TOKENS', 2)
    with pytest.raises(ValueError, match='split into smaller passages'):
        text.rerank(parameters)
