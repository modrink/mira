"""Bounded multi-stage human-pattern synthesis (classify → extract → cluster)."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.analysis.learned_rules import (
    rule_text_from_synth_action,
    sanitize_path_hint,
    unpack_learned_rule,
)
from mira.llm.utils import strip_code_fences, strip_think_blocks

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "llm" / "prompts" / "templates"

_EXTRACT_CAP = int(os.environ.get("MIRA_HUMAN_SYNTH_EXTRACT_CAP", "25"))
_CLASSIFY_BATCH = int(os.environ.get("MIRA_HUMAN_SYNTH_CLASSIFY_BATCH", "25"))
_EXTRACT_BATCH = int(os.environ.get("MIRA_HUMAN_SYNTH_EXTRACT_BATCH", "10"))
_CONCRETE_TOKEN_RE = re.compile(r"`[^`]+`|\(\)|->|::|\.\w+\(")

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


def comments_for_prompt(comments: list[dict]) -> list[dict]:
    """Attach 1-based index fields for classify/extract prompts."""
    out = []
    for i, c in enumerate(comments, start=1):
        out.append({**c, "index": i})
    return out


async def classify_comments(
    llm: Any,
    comments: list[dict],
    *,
    on_progress: ProgressCb | None = None,
) -> set[int]:
    """Return 1-based indexes marked EXTRACT."""
    if not comments:
        return set()
    env = _jinja_env()
    template = env.get_template("synthesize_classify.jinja2")
    extract_ids: set[int] = set()
    indexed = comments_for_prompt(comments)
    batch_size = max(1, _CLASSIFY_BATCH)
    total_batches = max(1, (len(indexed) + batch_size - 1) // batch_size)
    _emit(
        on_progress,
        phase="classify",
        classify_done=0,
        classify_total=total_batches,
    )
    done = 0
    for start in range(0, len(indexed), batch_size):
        batch = indexed[start : start + batch_size]
        prompt = template.render(comments=batch)
        data = await _llm_json(llm, prompt)
        if data:
            results = data.get("results")
            if isinstance(results, list):
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    try:
                        idx = int(item.get("index"))
                    except (TypeError, ValueError):
                        continue
                    decision = str(item.get("decision") or "").strip().upper()
                    if decision == "EXTRACT":
                        extract_ids.add(idx)
        done += 1
        _emit(
            on_progress,
            phase="classify",
            classify_done=done,
            classify_total=total_batches,
        )
    return extract_ids


def _prefer_hunk_then_cap(
    comments: list[dict],
    extract_indexes: set[int],
    *,
    cap: int,
) -> list[dict]:
    """Pick EXTRACT comments for extract stage; hunk-bearing first, then cap."""
    chosen: list[dict] = []
    with_hunk: list[dict] = []
    without: list[dict] = []
    for i, c in enumerate(comments, start=1):
        if i not in extract_indexes:
            continue
        row = {**c, "index": i}
        if (c.get("code") or "").strip():
            with_hunk.append(row)
        else:
            without.append(row)
    for row in with_hunk + without:
        chosen.append(row)
        if len(chosen) >= cap:
            break
    return chosen


async def extract_candidates(
    llm: Any,
    comments: list[dict],
    *,
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
    survivors: list[dict] = []
    dropped = 0
    total = len(comments)
    _emit(on_progress, phase="extract", extract_done=0, extract_total=total)
    processed = 0
    for start in range(0, len(comments), max(1, _EXTRACT_BATCH)):
        batch = comments[start : start + _EXTRACT_BATCH]
        prompt = template.render(comments=batch)
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
                    # Default prs to this comment's PR when model omits them.
                    prs = item.get("prs")
                    if not isinstance(prs, list) or not prs:
                        prn = int(src.get("pr_number") or 0)
                        item = {**item, "prs": [prn] if prn > 0 else []}
                    if not str(item.get("path_hint") or "").strip() and src.get("path"):
                        pass  # do not invent globs from a single path
                    rule_text = rule_text_from_synth_action(item)
                    if not rule_text:
                        dropped += 1
                        continue
                    title, body = unpack_learned_rule(rule_text)
                    # Hunk present but no fenced evidence and no concrete token → drop.
                    if (src.get("code") or "").strip() and (
                        "```" not in body and not _CONCRETE_TOKEN_RE.search(body)
                    ):
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
        rejected=rejected or [],
        max_rules=max_rules,
    )
    data = await _llm_json(llm, prompt)
    if not data:
        return []
    actions = data.get("actions")
    if not isinstance(actions, list):
        # Legacy {"rules": [...]}
        legacy = data.get("rules")
        if isinstance(legacy, list):
            return [
                {
                    "action": "create",
                    "rule": item.get("rule") if isinstance(item, dict) else "",
                    "evidence_count": item.get("evidence_count") if isinstance(item, dict) else 0,
                    "prs": item.get("prs") if isinstance(item, dict) else [],
                }
                for item in legacy
            ]
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
    """Full classify → extract → cluster pipeline. Returns action dicts."""
    if len(comments) < 2:
        return []

    cap = _EXTRACT_CAP if extract_cap is None else extract_cap
    extract_ids = await classify_comments(llm, comments, on_progress=on_progress)
    logger.info(
        "Human synth classify: comments=%d extract=%d",
        len(comments),
        len(extract_ids),
    )
    if not extract_ids:
        _emit(
            on_progress,
            phase="complete",
            classified=len(comments),
            extract=0,
            dropped_gate=0,
        )
        return []

    to_extract = _prefer_hunk_then_cap(comments, extract_ids, cap=max(1, cap))
    survivors, dropped = await extract_candidates(llm, to_extract, on_progress=on_progress)
    logger.info(
        "Human synth extract: attempted=%d survived=%d dropped_gate=%d",
        len(to_extract),
        len(survivors),
        dropped,
    )
    if not survivors:
        _emit(
            on_progress,
            phase="complete",
            classified=len(comments),
            extract=0,
            dropped_gate=dropped,
        )
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
    _emit(
        on_progress,
        phase="cluster",
        classified=len(comments),
        extract=len(survivors),
        dropped_gate=dropped,
    )
    return actions
