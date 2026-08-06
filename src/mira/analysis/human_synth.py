"""Human-pattern synthesis: extract → cluster."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.analysis.learned_rules import (
    body_passes_synth_gate,
    rule_text_from_synth_action,
    sanitize_path_hint,
    unpack_learned_rule,
)
from mira.llm.utils import strip_code_fences, strip_think_blocks

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "llm" / "prompts" / "templates"

_EXTRACT_CAP = int(os.environ.get("MIRA_HUMAN_SYNTH_EXTRACT_CAP", "80"))
_EXTRACT_BATCH = int(os.environ.get("MIRA_HUMAN_SYNTH_EXTRACT_BATCH", "10"))
_REJECTED_PROMPT_CAP = 20
_REJECTED_TEXT_CHARS = 200

# Cold-start anti-examples (failure classes). Shown even with empty rejected catalog.
_SEED_ANTI_EXAMPLES: list[dict[str, str]] = [
    {
        "class": "clarifying question",
        "example": "Should we filter by $environment here?",
    },
    {
        "class": "one-shot file chore",
        "example": "Append '.zip' to this path if it is missing.",
    },
    {
        "class": "non-actionable hedge",
        "example": "Check if filtering is required in the context.",
    },
    {
        "class": "title paraphrase",
        "example": "Use empty() for readability. Prefer empty() for better readability.",
    },
]

ProgressCb = Callable[[dict[str, Any]], None]


def _emit(on_progress: ProgressCb | None, **fields: Any) -> None:
    if not on_progress:
        return
    try:
        on_progress(fields)
    except Exception:
        logger.debug("Human synth progress callback failed", exc_info=True)


def _as_positive_int(value: object) -> int | None:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _parse_json_object(raw: str) -> dict:
    data = json.loads(strip_think_blocks(strip_code_fences(raw)))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


async def _llm_json(llm: Any, prompt: str) -> dict | None:
    try:
        raw = await llm.complete(
            messages=[{"role": "user", "content": prompt}],
            json_mode=True,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("Human synth LLM call failed: %s", exc)
        return None
    try:
        return _parse_json_object(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Human synth LLM returned bad JSON: %s", exc)
        return None


def _comments_for_extract(comments: list[dict], *, cap: int) -> list[dict]:
    """Index comments; prefer hunk-bearing; cap count."""
    with_hunk: list[dict] = []
    without: list[dict] = []
    for i, c in enumerate(comments, start=1):
        row = {**c, "index": i}
        if (c.get("code") or "").strip():
            with_hunk.append(row)
        else:
            without.append(row)
    chosen: list[dict] = []
    for row in with_hunk + without:
        chosen.append(row)
        if len(chosen) >= cap:
            break
    return chosen


def _rejected_for_prompt(rejected: list[dict] | None) -> list[dict]:
    """Newest-first cap; truncate rule_text for prompt budget."""
    rows = list(rejected or [])
    # Prefer higher ids when present (newer rows).
    rows.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
    out: list[dict] = []
    for row in rows[:_REJECTED_PROMPT_CAP]:
        text = str(row.get("rule_text") or "").strip()
        if not text:
            continue
        if len(text) > _REJECTED_TEXT_CHARS:
            text = text[:_REJECTED_TEXT_CHARS].rstrip() + "…"
        out.append({"id": row.get("id"), "rule_text": text})
    return out


def _source_is_question(body: str) -> bool:
    return (body or "").strip().endswith("?")


async def extract_candidates(
    llm: Any,
    comments: list[dict],
    *,
    rejected: list[dict] | None = None,
    on_progress: ProgressCb | None = None,
) -> tuple[list[dict], int]:
    """Structured extract + pack/gate.

    Returns ``(survivors, dropped_gate_count)``.
    """
    if not comments:
        return [], 0
    env = _jinja_env()
    template = env.get_template("synthesize_extract.jinja2")
    by_index = {int(c["index"]): c for c in comments}
    rejected_prompt = _rejected_for_prompt(rejected)
    survivors: list[dict] = []
    dropped = 0
    total = len(comments)
    _emit(on_progress, phase="extract", extract_done=0, extract_total=total)
    for start in range(0, len(comments), max(1, _EXTRACT_BATCH)):
        batch = comments[start : start + _EXTRACT_BATCH]
        prompt = template.render(
            comments=batch,
            seed_anti_examples=_SEED_ANTI_EXAMPLES,
            rejected=rejected_prompt,
        )
        data = await _llm_json(llm, prompt)
        if data:
            extractions = data.get("extractions")
            if isinstance(extractions, list):
                for item in extractions:
                    if not isinstance(item, dict):
                        continue
                    try:
                        idx = int(item.get("index"))
                    except (TypeError, ValueError):
                        continue
                    src = by_index.get(idx)
                    if src is None:
                        continue
                    if _source_is_question(str(src.get("body") or "")):
                        dropped += 1
                        continue
                    prs = item.get("prs")
                    if not isinstance(prs, list) or not prs:
                        prn = int(src.get("pr_number") or 0)
                        item = {**item, "prs": [prn] if prn > 0 else []}
                    rule_text = rule_text_from_synth_action(item)
                    if not rule_text:
                        dropped += 1
                        continue
                    title, body = unpack_learned_rule(rule_text)
                    if not body_passes_synth_gate(title, body):
                        dropped += 1
                        continue
                    survivors.append(
                        {
                            "id": len(survivors) + 1,
                            "comment_index": idx,
                            "title": title,
                            "body": body,
                            "rule_text": rule_text,
                            "path_hint": sanitize_path_hint(str(item.get("path_hint") or "")),
                            "path": str(src.get("path") or ""),
                            "prs": [
                                int(p)
                                for p in (item.get("prs") or [])
                                if _as_positive_int(p) is not None
                            ],
                            "has_hunk": bool((src.get("code") or "").strip()),
                        }
                    )
        processed = min(total, start + len(batch))
        _emit(
            on_progress,
            phase="extract",
            extract_done=processed,
            extract_total=total,
        )
    if dropped:
        logger.info("Human synth extract gate dropped %d candidates", dropped)
    return survivors, dropped


async def cluster_candidates(
    llm: Any,
    *,
    extractions: list[dict],
    catalog: list[dict],
    max_rules: int,
    rejected: list[dict] | None = None,
) -> list[dict]:
    """One catalog-aware merge call → list of action dicts."""
    if not extractions:
        return []
    env = _jinja_env()
    template = env.get_template("synthesize_cluster.jinja2")
    prompt = template.render(
        extractions=extractions,
        catalog=catalog,
        rejected=_rejected_for_prompt(rejected),
        seed_anti_examples=_SEED_ANTI_EXAMPLES,
        max_rules=max_rules,
    )
    data = await _llm_json(llm, prompt)
    if not data:
        return []
    actions = data.get("actions")
    if not isinstance(actions, list):
        return []
    return [a for a in actions if isinstance(a, dict)]


async def run_staged_human_synth(
    llm: Any,
    *,
    comments: list[dict],
    catalog: list[dict],
    max_rules: int,
    extract_cap: int | None = None,
    on_progress: ProgressCb | None = None,
    rejected: list[dict] | None = None,
) -> list[dict]:
    """Extract → cluster. Returns action dicts."""
    if len(comments) < 2:
        return []

    cap = _EXTRACT_CAP if extract_cap is None else extract_cap
    to_extract = _comments_for_extract(comments, cap=max(1, cap))
    survivors, dropped = await extract_candidates(
        llm,
        to_extract,
        rejected=rejected,
        on_progress=on_progress,
    )
    logger.info(
        "Human synth extract: attempted=%d survived=%d dropped_gate=%d",
        len(to_extract),
        len(survivors),
        dropped,
    )
    if not survivors:
        _emit(on_progress, phase="complete", llm_rules=0)
        return []

    _emit(on_progress, phase="cluster")
    actions = await cluster_candidates(
        llm,
        extractions=survivors,
        catalog=catalog,
        max_rules=max_rules,
        rejected=rejected,
    )
    logger.info("Human synth cluster: actions=%d", len(actions))
    _emit(on_progress, phase="cluster")
    return actions
