"""Default model resolution for the dashboard model pickers."""

from __future__ import annotations

from mira.config import LLMConfig
from mira.dashboard.models_config import (
    estimate_learnings_backfill_cost,
    get_indexing_model,
    get_review_model,
)


def test_indexing_falls_back_to_config_model():
    # No override anywhere → the configured model is honored as-is, even if
    # it's review-tier; the combobox accepts free-form ids so nothing needs
    # to match a registry entry.
    cfg = LLMConfig()
    assert get_indexing_model(cfg) == cfg.model


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


def test_learnings_backfill_cost_zero_repos():
    est = estimate_learnings_backfill_cost(0, "anthropic/claude-haiku-4-5")
    assert est["estimated_usd"] == 0.0
    assert est["input_tokens"] == 0
    assert est["output_tokens"] == 0
    assert est["synth_calls"] == 0


def test_learnings_backfill_cost_scales_with_repos():
    one = estimate_learnings_backfill_cost(1, "anthropic/claude-haiku-4-5")
    three = estimate_learnings_backfill_cost(3, "anthropic/claude-haiku-4-5")
    assert one["input_tokens"] == 16_000
    assert one["output_tokens"] == 2_000
    assert one["synth_calls"] == 1
    assert three["input_tokens"] == 3 * one["input_tokens"]
    assert three["output_tokens"] == 3 * one["output_tokens"]
    assert three["estimated_usd"] >= one["estimated_usd"]


def test_estimate_repo_synth_tokens_skips_thin_signal():
    from mira.dashboard.models_config import estimate_repo_synth_tokens

    assert estimate_repo_synth_tokens(human_review_count=0, catalog_count=0, max_prs=5) is None
    assert estimate_repo_synth_tokens(human_review_count=1, catalog_count=0, max_prs=10) is None
    cold = estimate_repo_synth_tokens(human_review_count=0, catalog_count=0, max_prs=100)
    assert cold is not None
    rich = estimate_repo_synth_tokens(human_review_count=50, catalog_count=20, max_prs=100)
    assert rich is not None
    assert rich[0] > cold[0]
