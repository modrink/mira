"""Model resolution — reads from DB settings first, falls back to config.

Model lists, pricing, and capabilities all come from
``src/mira/llm/models.json`` via ``mira.llm.registry``. Add or remove a
model there; this file picks it up automatically.
"""

from __future__ import annotations

import logging

from mira.config import LLMConfig
from mira.llm import registry

logger = logging.getLogger(__name__)

MODEL_PRICING: dict[str, tuple[float, float]] = {
    model_id: registry.pricing(model_id) for model_id in registry.all_models()
}

# Thinking-mode options for the review model. "off" disables extended thinking
# (today's behavior); low/medium/high map to OpenRouter's unified
# ``reasoning.effort``. Single source for the dashboard dropdown and validation.
THINKING_MODES: list[dict[str, str]] = [
    {"value": "off", "label": "Off"},
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
    # DeepSeek's top "max" level (sent as "xhigh" on OpenRouter, which rejects
    # "max"). Not every provider supports it.
    {"value": "max", "label": "Max"},
]
THINKING_MODE_VALUES = {m["value"] for m in THINKING_MODES}


def estimate_indexing_cost(file_count: int, model: str) -> dict:
    """Estimate cost of indexing N files with the given model.

    Based on actual indexer behavior:
    - Files batched 5-at-a-time
    - Each batch uses ~4K input tokens (prompt + 5 file contents ~500 lines avg)
    - Each batch outputs ~2K tokens (summaries + symbols JSON)
    - Plus a directory summarization pass at the end (~1 call per 10 files)
    """
    if file_count == 0:
        return {"estimated_usd": 0.0, "input_tokens": 0, "output_tokens": 0}

    input_price, output_price = MODEL_PRICING.get(model, (3.00, 15.00))

    # File summarization batches
    batches = (file_count + 4) // 5  # ceil div
    # Estimate: 800 tokens per file input, 400 tokens per file output
    input_tokens = file_count * 800 + batches * 500  # +prompt overhead per batch
    output_tokens = file_count * 400

    # Directory summarization pass
    dir_batches = max(1, file_count // 10)
    input_tokens += dir_batches * 1500
    output_tokens += dir_batches * 300

    cost = (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price

    return {
        "estimated_usd": round(cost, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def estimate_learnings_backfill_cost(
    repo_count: int,
    model: str,
    *,
    synth_calls: int | None = None,
    avg_input_tokens: int = 16_000,
    avg_output_tokens: int = 2_000,
) -> dict:
    """Estimate LLM cost of learnings backfill.

    PR ingest is GitHub API only. LLM cost is staged synthesis per repo that
    has enough human_review signal (extract batches + one cluster call;
    worst case: every repo).

    Pass ``synth_calls`` when known from store stats; otherwise assume one
    *repo* synth (token averages should reflect the staged pipeline).
    ``avg_*_tokens`` are fallbacks when per-repo estimates are unavailable.
    """
    calls = repo_count if synth_calls is None else max(0, synth_calls)
    if calls <= 0:
        return {
            "estimated_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "synth_calls": 0,
        }

    input_price, output_price = MODEL_PRICING.get(model, (3.00, 15.00))
    input_tokens = calls * max(1, avg_input_tokens)
    output_tokens = calls * max(1, avg_output_tokens)
    cost = (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
    return {
        "estimated_usd": round(cost, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "synth_calls": calls,
    }


def estimate_repo_synth_tokens(
    *,
    human_review_count: int,
    catalog_count: int,
    max_prs: int,
) -> tuple[int, int] | None:
    """Per-repo (input, output) tokens for staged human synth, or None if skip.

    Uses stored feedback when present; for cold repos assumes a mid-size sample
    proportional to ``max_prs`` (backfill will ingest new human comments).

    Stages: capped extract + one catalog cluster call.
    """
    # Caps match mira.analysis.feedback / human_synth defaults.
    max_comments = 100
    max_rules = 40
    extract_cap = 80
    extract_batch = 10

    projected = human_review_count
    if projected < 2 and max_prs >= 20:
        # First-ish fill: expect some line comments across the window.
        projected = min(max_comments, max(8, max_prs // 5))
    if projected < 2:
        return None

    comments = min(max_comments, projected)
    extracts = min(extract_cap, comments)
    extract_calls = max(1, (extracts + extract_batch - 1) // extract_batch)

    input_tokens = (
        extract_calls * (1_000 + extract_batch * 160)
        + (2_200 + extracts * 70 + max(0, catalog_count) * 45)
    )
    output_tokens = extract_calls * 350 + (400 + max_rules * 180)
    return input_tokens, output_tokens


def get_indexing_model(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve the indexing model: DB → config.indexing_model → config.model."""
    if db_value:
        return db_value
    if config.indexing_model:
        return config.indexing_model
    return config.model


def get_review_model(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve the review model: DB → config.review_model → config.model."""
    if db_value:
        return db_value
    if config.review_model:
        return config.review_model
    return config.model


def get_review_thinking_mode(config: LLMConfig, db_value: str | None = None) -> str | None:
    """Resolve the review thinking mode: DB → config.review_reasoning_effort → None.

    A DB value of "off" or "" counts as unset and falls through to the
    mira.yaml-level setting — saving the models form always writes this key
    (default "off"), so a stored "off" must not permanently shadow a config
    override. "off" anywhere normalizes to None ("no reasoning").
    """
    resolved = db_value if (db_value and db_value != "off") else config.review_reasoning_effort
    if not resolved or resolved == "off":
        return None
    return resolved


def llm_config_for(purpose: str, base: LLMConfig) -> LLMConfig:
    """Return an LLMConfig with the appropriate model set for the given purpose.

    Reads the DB setting first (via _app_db), falls back to config fields.
    Logs the effective model and where it came from, so a dashboard override
    shadowing mira.yaml is visible instead of silent (issue #124).
    """
    db_model: str | None = None
    db_thinking: str | None = None
    try:
        from mira.dashboard.api import _app_db

        if _app_db is not None:
            if purpose == "indexing":
                db_model = _app_db.get_setting("indexing_model")
            elif purpose == "review":
                db_model = _app_db.get_setting("review_model")
                db_thinking = _app_db.get_setting("review_thinking_mode")
    except Exception:
        pass  # DB not available — resolve from config fields alone

    # Thinking mode only applies to reviews; other purposes leave it off.
    thinking_mode: str | None = None
    if purpose == "indexing":
        resolved = get_indexing_model(base, db_model)
        config_model = base.indexing_model
    elif purpose == "review":
        resolved = get_review_model(base, db_model)
        config_model = base.review_model
        thinking_mode = get_review_thinking_mode(base, db_thinking)
    else:
        return base.model_copy(update={"reasoning_effort": None})

    source = "dashboard setting" if db_model else ("mira.yaml" if config_model else "default")
    logger.info("%s model: %s (source: %s)", purpose.capitalize(), resolved, source)
    return base.model_copy(update={"model": resolved, "reasoning_effort": thinking_mode})
