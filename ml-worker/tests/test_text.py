"""Verify model-specific formatting, last-token pooling, and causal scoring."""
from types import SimpleNamespace
import pytest
from ml_worker import text


def test_embeddings_and_reranking_use_pinned_model_conventions(monkeypatch):
    torch = pytest.importorskip('torch')
    calls = []
    class Batch(dict):
        def to(self, device): return self
    class Tokenizer:
        def __call__(self, inputs, **kwargs):
            calls.append(inputs[0])
            # Distinguish candidates while retaining single-item inference.
            return Batch(input_ids=torch.tensor([[1, 2, int('beta' in inputs[0]) + 3]]))
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
                logits = torch.zeros((1, 1, text.YES_TOKEN + 1))
                logits[0, -1, text.YES_TOKEN] = input_ids[0, -1]
                return SimpleNamespace(logits=logits)
            hidden = torch.full((1, 3, text.DIMENSIONS), float('nan'))
            hidden[:, -1, :] = 1
            return SimpleNamespace(last_hidden_state=hidden)
    def load(model_id, revision, *, classifier, device):
        assert (model_id, revision) == ((text.RERANK_MODEL, text.RERANK_REVISION) if classifier
                                       else (text.EMBED_MODEL, text.EMBED_REVISION))
        return Tokenizer(), Model(classifier), 'cpu'
    monkeypatch.setattr(text, '_load', load)
    items = [{'id':'a', 'text':'alpha'}, {'id':'b', 'text':'beta'}]
    result = text.embeddings({'texts':items})
    assert calls[0] == '<|im_start|>system\ndocument<|im_end|>\n<|im_start|>user\nalpha<|im_end|>\n'
    assert result['space']['dimensions'] == text.DIMENSIONS
    assert result['space']['recipe'] == text.EMBED_RECIPE
    assert abs(sum(x*x for x in result['records'][0]['vector'])-1) < 1e-5
    text.embeddings({'texts':items, 'input_type':'query'})
    assert calls[-2] == '<|im_start|>system\nquery<|im_end|>\n<|im_start|>user\nalpha<|im_end|>\n'
    result = text.rerank({'texts':items, 'query':'release', 'top_n':1})
    assert calls[-1] == '<|im_start|>system\nrelease<|im_end|>\n<|im_start|>user\nbeta<|im_end|>\n<|im_start|>assistant\n'
    assert result['results'] == [{'index':1, 'id':'b', 'relevance_score':4.0}]
    with pytest.raises(ValueError): text.embeddings({'texts':items, 'namespace':'speakers'})
    with pytest.raises(ValueError): text.rerank({'texts':items, 'query':'release', 'top_n':True})
    with pytest.raises(ValueError): text.records([items[0],items[0]])
    monkeypatch.setattr(text, 'MAX_TOKENS', 2)
    with pytest.raises(ValueError, match='split into smaller passages'):
        text.embeddings({'texts':items})
