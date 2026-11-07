"""Verify model-specific formatting, last-token pooling, and causal scoring."""
from types import SimpleNamespace
import pytest
from ml_worker import text


def test_embeddings_and_reranking_use_pinned_model_conventions(monkeypatch):
    torch = pytest.importorskip('torch')
    calls = []
    batches = []
    class Batch(dict):
        def to(self, device): return self
    class Tokenizer:
        pad_token_id = 0
        def __call__(self, inputs, **kwargs):
            calls.extend(inputs)
            return {'input_ids': [[1] * (2 + int('beta' in value)) +
                                  [int('beta' in value) + 3] for value in inputs]}
        def pad(self, encoded, *, padding, return_tensors):
            assert self.padding_side == 'left' and padding and return_tensors == 'pt'
            rows = encoded['input_ids']
            length = max(map(len, rows))
            ids = torch.tensor([[0] * (length - len(row)) + row for row in rows])
            batches.append(ids.shape)
            return Batch(input_ids=ids, attention_mask=(ids != 0).long())
        def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
            assert not tokenize and add_generation_prompt
            assert [m['role'] for m in messages] == ['query', 'document']
            return f"<|im_start|>system\n{messages[0]['content']}<|im_end|>\n<|im_start|>user\n{messages[1]['content']}<|im_end|>\n<|im_start|>assistant\n"
    class Model:
        def __init__(self, classifier): self.classifier = classifier
        def __call__(self, input_ids, *, use_cache, **kwargs):
            assert not use_cache
            if self.classifier:
                assert kwargs['logits_to_keep'] == 1
                logits = torch.zeros((len(input_ids), 1, text.YES_TOKEN + 1))
                logits[:, -1, text.YES_TOKEN] = input_ids[:, -1]
                return SimpleNamespace(logits=logits)
            hidden = torch.full((*input_ids.shape, text.DIMENSIONS), float('nan'))
            hidden[:, -1, :] = 1
            return SimpleNamespace(last_hidden_state=hidden)
    def load(model_id, revision, *, classifier, device):
        assert (model_id, revision) == ((text.RERANK_MODEL, text.RERANK_REVISION) if classifier
                                       else (text.EMBED_MODEL, text.EMBED_REVISION))
        return Tokenizer(), Model(classifier), 'cpu'
    monkeypatch.setattr(text, '_load', load)
    items = [{'id':'a', 'text':'alpha'}, {'id':'b', 'text':'beta'}]
    result = text.embeddings({'texts':items})
    embedded = result
    assert calls[0] == '<|im_start|>system\ndocument<|im_end|>\n<|im_start|>user\nalpha<|im_end|>\n'
    assert result['space']['dimensions'] == text.DIMENSIONS
    assert result['space']['recipe'] == text.EMBED_RECIPE
    assert abs(sum(x*x for x in result['records'][0]['vector'])-1) < 1e-5
    text.embeddings({'texts':items, 'input_type':'query'})
    assert calls[-2] == '<|im_start|>system\nquery<|im_end|>\n<|im_start|>user\nalpha<|im_end|>\n'
    result = text.rerank({'texts':items, 'query':'release', 'top_n':1})
    assert calls[-1] == '<|im_start|>system\nrelease<|im_end|>\n<|im_start|>user\nbeta<|im_end|>\n<|im_start|>assistant\n'
    assert result['results'] == [{'index':1, 'id':'b', 'relevance_score':4.0}]
    assert batches == [(2, 4)] * 3
    # The same request through single-item calls preserves vectors and ranking.
    monkeypatch.setattr(text, 'INFERENCE_BATCH_SIZE', 1)
    assert text.rerank({'texts':items, 'query':'release', 'top_n':1}) == result
    assert text.embeddings({'texts':items}) == embedded
    monkeypatch.setattr(text, 'INFERENCE_BATCH_SIZE', 8)
    monkeypatch.setattr(text, 'INFERENCE_BATCH_TOKENS', 6)
    assert text.rerank({'texts':items, 'query':'release', 'top_n':1}) == result
    assert batches[-2:] == [(1, 3), (1, 4)]
    monkeypatch.setattr(text, 'INFERENCE_BATCH_TOKENS', 2048)
    many = [{'id':str(i), 'text':'beta' if i == 29 else 'alpha'} for i in range(30)]
    ranked = text.rerank({'texts':many, 'query':'release'})
    assert [r['index'] for r in ranked['results']] == [29, *range(29)]
    assert batches[-4:] == [(8, 3), (8, 3), (8, 3), (6, 4)]
    with pytest.raises(ValueError): text.embeddings({'texts':items, 'namespace':'speakers'})
    with pytest.raises(ValueError): text.rerank({'texts':items, 'query':'release', 'top_n':True})
    with pytest.raises(ValueError): text.records([items[0],items[0]])
    monkeypatch.setattr(text, 'MAX_TOKENS', 2)
    with pytest.raises(ValueError, match='split into smaller passages'):
        text.embeddings({'texts':items})
