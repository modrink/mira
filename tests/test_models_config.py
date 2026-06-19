"""Default model resolution for the setup dropdowns."""

from __future__ import annotations

from mira.config import LLMConfig
from mira.dashboard.models_config import get_indexing_model, get_review_model
from mira.llm import registry


def test_indexing_default_is_a_valid_indexing_model():
    # Default config.model is review-tier Sonnet, which is NOT an indexing
    # option; the resolver must fall back to a real indexing model so the
    # dropdown has something to pre-select.
    resolved = get_indexing_model(LLMConfig())
    assert registry.is_supported(resolved, purpose="indexing")
    assert resolved == registry.default_for_purpose("indexing")


def test_review_default_unchanged_when_model_is_review_capable():
    assert get_review_model(LLMConfig()) == "anthropic/claude-sonnet-4-6"


def test_explicit_choices_are_respected():
    # DB value wins outright.
    assert get_indexing_model(LLMConfig(), db_value="openai/gpt-4o-mini") == "openai/gpt-4o-mini"
    # config.indexing_model wins over the generic fallback, even if custom.
    assert get_indexing_model(LLMConfig(indexing_model="custom/local")) == "custom/local"


def test_indexing_capable_model_passes_through():
    # If config.model is itself indexing-capable, keep it.
    cfg = LLMConfig(model="anthropic/claude-haiku-4-5")
    assert get_indexing_model(cfg) == "anthropic/claude-haiku-4-5"
