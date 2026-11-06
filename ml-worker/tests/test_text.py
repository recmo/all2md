"""Verify real tensor pooling and ranking with tiny in-memory model outputs."""
from types import SimpleNamespace
import pytest
from ml_worker import text


def test_embeddings_and_reranking_use_documented_inputs_and_masking(monkeypatch):
    torch = pytest.importorskip('torch')
    calls = []
    class Batch(dict):
        def to(self, device): return self
    class Tokenizer:
        def __call__(self, first, second=None, **kwargs):
            calls.append((first, second, kwargs))
            return Batch(attention_mask=torch.tensor([[1, 1, 0]]*len(first)))
    class Model:
        def __init__(self, classifier): self.classifier = classifier
        def __call__(self, attention_mask):
            if self.classifier:
                return SimpleNamespace(logits=torch.arange(len(attention_mask), dtype=torch.float32)[:, None])
            hidden = torch.ones((len(attention_mask), 3, 384))
            hidden[:, 2, :] = float('nan') # Padding must be excluded, not multiplied by zero.
            return SimpleNamespace(last_hidden_state=hidden)
    def load(*args, classifier, device):
        return Tokenizer(), Model(classifier), 'cpu'
    monkeypatch.setattr(text, '_load', load)
    items = [{'id':'a', 'text':'alpha'}, {'id':'b', 'text':'beta'}]
    result = text.embeddings({'texts':items})
    assert calls[-1][0] == ['passage: alpha', 'passage: beta']
    assert result['space']['dimensions'] == 384
    assert abs(sum(x*x for x in result['records'][0]['vector'])-1) < 1e-5
    text.embeddings({'texts':items, 'input_type':'query'})
    assert calls[-1][0] == ['query: alpha', 'query: beta']
    result = text.rerank({'texts':items, 'query':'beta', 'top_n':1})
    assert calls[-1][1] == ['alpha','beta']
    assert result['results'] == [{'index':1, 'id':'b', 'relevance_score':1.0}]
    with pytest.raises(ValueError): text.embeddings({'texts':items, 'namespace':'speakers'})
    with pytest.raises(ValueError): text.rerank({'texts':items, 'query':'beta', 'top_n':True})
    with pytest.raises(ValueError): text.records([items[0],items[0]])
