"""Deterministic settings and tokenizer shared by pipeline regression tests."""

from app.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "api_key": "test-api-key-padded-to-min-length-x",
        "embedding_api_base_url": "http://fake:8080",
        "embedding_api_key": "k",
        "embedding_model": "intfloat/multilingual-e5-large",
        "embedding_dim": 1024,
    }
    base.update(overrides)
    return Settings(**base)


class _FakeTokenizer:
    """Stand-in for a HuggingFace tokenizer: one token per whitespace word.

    Makes every budget assertion below computable by hand. ``add_special_tokens``
    is accepted and ignored because the splitters always pass ``False`` and
    account for specials separately via ``num_special_tokens_to_add``.
    """

    def encode(self, text, add_special_tokens=False):
        return text.split()

    def tokenize(self, text):
        return text.split()

    def num_special_tokens_to_add(self):
        return 2  # <s> ... </s>, as on XLM-R / e5
