"""Synthesise learned rules from accumulated feedback events."""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

from mira.analysis.learned_rules import (
    find_near_duplicate_rule,
    human_pattern_path,
    normalize_rule_text,
    pack_learned_rule,
    rule_text_from_synth_action,
    sanitize_path_hint,
)
from mira.index.store import IndexStore

logger = logging.getLogger(__name__)

# Minimum reject count before we generate a rule for a (category, dir) pair.
_MIN_REJECTS_PER_DIR = int(os.environ.get("MIRA_FEEDBACK_MIN_DIR", "3"))
# Minimum reject count across all paths for a category-wide rule.
_MIN_REJECTS_CATEGORY = int(os.environ.get("MIRA_FEEDBACK_MIN_CAT", "5"))
# Max human-review comments to include in a single LLM synthesis call.
_MAX_HUMAN_COMMENTS = int(os.environ.get("MIRA_HUMAN_SYNTH_MAX", "100"))
# Max catalog actions the LLM is allowed to emit per synthesis run.
_MAX_LLM_RULES = int(os.environ.get("MIRA_HUMAN_SYNTH_MAX_RULES", "40"))
# How many feedback rows to load before filtering humans (accept/reject noise).
_FEEDBACK_FETCH_LIMIT = int(os.environ.get("MIRA_HUMAN_SYNTH_FETCH", "2000"))
# Live-merge LLM synth cooldown (seconds). Deterministic reject synth always runs.
_LLM_SYNTH_COOLDOWN_SEC = int(os.environ.get("MIRA_LEARN_SYNTH_COOLDOWN_SEC", "3600"))
_LLM_SYNTH_SIGNAL = "learn_synth"

# Pack diff hunk into feedback comment_title for synth (no schema migration).
_CODE_MARK_START = "\n\n[code]\n"
_CODE_MARK_END = "\n[/code]"


def pack_human_comment_for_learning(body: str, diff_hunk: str = "") -> str:
    """Store reviewer text + optional diff hunk in the feedback title field."""
    body = (body or "").strip()
    hunk = (diff_hunk or "").strip()
    if not hunk:
        return body[:2000]
    overhead = len(_CODE_MARK_START) + len(_CODE_MARK_END)
    room = 2000 - overhead
    if room < 80:
        return body[:2000]
    # Prefer reviewer words; leave remaining room for the hunk.
    max_body = min(len(body), 1400, room - 40)
    max_hunk = room - max_body
    if max_hunk < 40:
        return body[:2000]
    packed = f"{body[:max_body]}{_CODE_MARK_START}{hunk[:max_hunk]}{_CODE_MARK_END}"
    return packed[:2000]


def unpack_human_comment_for_synth(stored: str) -> tuple[str, str]:
    """Split packed ``comment_title`` into (body, code)."""
    text = stored or ""
    if _CODE_MARK_START not in text:
        return text, ""
    body, _, rest = text.partition(_CODE_MARK_START)
    code, _, _ = rest.partition(_CODE_MARK_END)
    return body.strip(), code.strip()


def _dir_of(path: str) -> str:
    """Extract the top-level directory from a file path, or '' for root files."""
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else ""


def _event_has_hunk(event: object) -> bool:
    """True when packed comment_title includes a non-empty [code] hunk."""
    _, code = unpack_human_comment_for_synth(str(getattr(event, "comment_title", "") or ""))
    return bool(code.strip())


def _select_human_comments_for_synth(events: list, limit: int) -> list:
    """Pick human_review comments for LLM synth via round-robin across PRs.

    Newest-first slicing by ``created_at`` fails after historical backfill:
    each PR batch shares one ingest timestamp, so the window favors the
    last-written PRs and starves fat earlier batches. Round-robin gives every
    PR a slot before any PR can dominate the prompt.

    Within each PR, prefer comments that carry a packed diff hunk so extract
    stages see concrete before/after symbols.
    """
    humans = [e for e in events if getattr(e, "signal", None) == "human_review"]
    if not humans or limit <= 0:
        return []

    by_pr: dict[int, list] = defaultdict(list)
    for e in humans:
        by_pr[int(e.pr_number)].append(e)

    for rows in by_pr.values():
        rows.sort(
            key=lambda e: (
                0 if _event_has_hunk(e) else 1,
                int(getattr(e, "id", 0) or 0),
            )
        )

    # Prefer newer PR batches for the first round of slots, then keep cycling.
    pr_order = sorted(
        by_pr.keys(),
        key=lambda pr: (
            max(float(getattr(e, "created_at", 0.0) or 0.0) for e in by_pr[pr]),
            max(int(getattr(e, "id", 0) or 0) for e in by_pr[pr]),
        ),
        reverse=True,
    )

    queues = {pr: list(by_pr[pr]) for pr in pr_order}
    selected: list = []
    while len(selected) < limit:
        progressed = False
        for pr in pr_order:
            if queues[pr] and len(selected) < limit:
                selected.append(queues[pr].pop(0))
                progressed = True
        if not progressed:
            break
    return selected


def _existing_human_patterns_by_text(store: IndexStore) -> dict[str, object]:
    """Map normalized rule text → existing ``human_pattern`` row."""
    found: dict[str, object] = {}
    for row in store.list_learned_rules():
        if row.source_signal != "human_pattern":
            continue
        found[normalize_rule_text(row.rule_text)] = row
    return found


def _catalog_for_synth(store: IndexStore) -> list[dict]:
    """Pending/approved human_pattern + manual rules for the catalog-aware prompt."""
    out: list[dict] = []
    for row in store.list_learned_rules():
        if row.status not in ("pending", "approved"):
            continue
        if row.source_signal not in ("human_pattern", "manual"):
            continue
        out.append(
            {
                "id": row.id,
                "status": row.status,
                "source_signal": row.source_signal,
                "rule_text": row.rule_text,
            }
        )
    return out


def _rejected_for_synth(store: IndexStore) -> list[dict]:
    """Rejected human_pattern / manual rules — do not recreate near-dupes."""
    out: list[dict] = []
    for row in store.list_learned_rules():
        if row.status != "rejected":
            continue
        if row.source_signal not in ("human_pattern", "manual"):
            continue
        out.append({"id": row.id, "rule_text": row.rule_text})
    return out


def clear_pending_synth_rules(store: IndexStore) -> int:
    """Delete auto-synthesized pending human_pattern rows (keep @remember / manual).

    Used on admin Rebuild so Pending reflects the new prompt quality instead of
    stacking on stale mush. Hand-authored pending (``created_by`` set) is kept.
    """
    cleared = 0
    for row in list(store.list_learned_rules(status="pending")):
        if row.source_signal != "human_pattern":
            continue
        if str(getattr(row, "created_by", "") or "").strip():
            continue
        store.delete_learned_rule(int(row.id))
        cleared += 1
    if cleared:
        logger.info("Cleared %d pending synth learnings before rebuild", cleared)
    return cleared


def llm_synth_cooldown_elapsed(store: IndexStore, *, now: float | None = None) -> bool:
    """True when live-merge LLM synth is allowed (cooldown elapsed or never ran)."""
    last = store.last_feedback_signal_at(_LLM_SYNTH_SIGNAL)
    if last <= 0:
        return True
    return (now if now is not None else time.time()) - last >= _LLM_SYNTH_COOLDOWN_SEC


def mark_llm_synth(store: IndexStore) -> None:
    """Record that an LLM catalog synth just ran (for live-merge debounce)."""
    store.record_feedback(
        pr_number=0,
        pr_url="",
        comment_path="__learn_synth__",
        comment_line=0,
        comment_category="",
        comment_severity="",
        comment_title="",
        signal=_LLM_SYNTH_SIGNAL,
        actor="system",
    )


def synthesize_rules(store: IndexStore) -> int:
    """Analyse feedback events and upsert reject_pattern learned rules.

    ``accept_pattern`` tautologies are no longer generated.

    Returns the number of rules created or updated.
    """
    events = store.list_feedback(limit=2000)
    if not events:
        return 0

    rejects_by_cat_dir: dict[tuple[str, str], int] = defaultdict(int)
    rejects_by_cat: dict[str, int] = defaultdict(int)
    prs_by_cat: dict[str, set[int]] = defaultdict(set)
    prs_by_cat_dir: dict[tuple[str, str], set[int]] = defaultdict(set)

    for ev in events:
        cat = ev.comment_category or "unknown"
        if cat == "unknown":
            continue
        if ev.signal == "rejected":
            directory = _dir_of(ev.comment_path)
            rejects_by_cat_dir[(cat, directory)] += 1
            rejects_by_cat[cat] += 1
            prn = int(getattr(ev, "pr_number", 0) or 0)
            if prn > 0:
                prs_by_cat[cat].add(prn)
                if directory:
                    prs_by_cat_dir[(cat, directory)].add(prn)

    upserted = 0

    # Category-wide reject rules (higher threshold)
    for cat, count in rejects_by_cat.items():
        if count < _MIN_REJECTS_CATEGORY:
            continue
        title = f"Raise the bar on {cat}"[:80]
        body = (
            f"This team frequently rejects '{cat}' suggestions ({count} rejections). "
            f"Raise the bar significantly for this category — only flag clear, "
            f"high-confidence issues."
        )
        packed = pack_learned_rule(title, body)
        if not packed:
            continue
        store.upsert_learned_rule(
            rule_text=packed,
            source_signal="reject_pattern",
            category=cat,
            path_pattern="",
            sample_count=count,
            evidence_prs=",".join(str(n) for n in sorted(prs_by_cat.get(cat, ()))),
        )
        upserted += 1

    # Per-directory reject rules (lower threshold)
    for (cat, directory), count in rejects_by_cat_dir.items():
        if not directory or count < _MIN_REJECTS_PER_DIR:
            continue
        # Skip if already covered by a category-wide rule
        if rejects_by_cat.get(cat, 0) >= _MIN_REJECTS_CATEGORY:
            continue
        title = f"Raise the bar on {cat} in {directory}"[:80]
        body = (
            f"Avoid '{cat}' comments on files in {directory}/ — this team has "
            f"rejected {count} such suggestions."
        )
        packed = pack_learned_rule(title, body)
        if not packed:
            continue
        store.upsert_learned_rule(
            rule_text=packed,
            source_signal="reject_pattern",
            category=cat,
            path_pattern=f"{directory}/**",
            sample_count=count,
            evidence_prs=",".join(str(n) for n in sorted(prs_by_cat_dir.get((cat, directory), ()))),
        )
        upserted += 1

    return upserted


def _apply_create(
    store: IndexStore,
    *,
    rule_text: str,
    evidence: int,
    by_text: dict[str, object],
    seen_batch: set[str],
    evidence_prs: str = "",
    catalog_rows: list | None = None,
    path_hint: str = "",
    rejected_rows: list | None = None,
) -> bool:
    rule_text = (rule_text or "").strip()
    if not rule_text:
        return False
    norm = normalize_rule_text(rule_text)
    if norm in seen_batch:
        return False
    seen_batch.add(norm)

    # Near-dupe of a rejected rule → skip (do not revive).
    if find_near_duplicate_rule(rule_text, list(rejected_rows or [])):
        logger.info("Skipping create near rejected learning: %s", rule_text[:80])
        return False

    # Near-dupe → merge into existing catalog row instead of minting another pending.
    near = find_near_duplicate_rule(rule_text, list(catalog_rows or by_text.values()))
    if near is not None:
        return _apply_merge(
            store,
            target_id=int(near.id),
            rule_text=rule_text,
            evidence=evidence,
            by_text=by_text,
            evidence_prs=evidence_prs,
        )

    existing = by_text.get(norm)
    if existing is not None:
        path_pattern = str(getattr(existing, "path_pattern", "") or "")
    else:
        path_pattern = human_pattern_path(rule_text, path_hint)

    row = store.upsert_learned_rule(
        rule_text=rule_text,
        source_signal="human_pattern",
        category="human_review",
        path_pattern=path_pattern,
        sample_count=max(1, evidence),
        evidence_prs=evidence_prs,
    )
    by_text[norm] = row
    return True


def _apply_merge(
    store: IndexStore,
    *,
    target_id: int,
    rule_text: str,
    evidence: int,
    by_text: dict[str, object],
    evidence_prs: str = "",
) -> bool:
    existing = store.get_learned_rule(target_id)
    if existing is None:
        return False
    if existing.source_signal not in ("human_pattern", "manual"):
        return False
    if existing.status not in ("pending", "approved"):
        return False

    rule_text = (rule_text or "").strip()
    sample_count = max(evidence, int(existing.sample_count or 0), 1)
    new_text = rule_text if existing.status == "pending" and rule_text else None
    # Pending: replace evidence with this action's PRs.
    # Approved: union newly cited PRs.
    row = store.bump_learned_rule_evidence(
        target_id,
        sample_count,
        rule_text=new_text,
        evidence_prs=evidence_prs,
        replace_evidence=bool(existing.status == "pending" and evidence_prs),
    )
    if row is None:
        return False
    by_text[normalize_rule_text(row.rule_text)] = row
    return True


def _prs_csv_from_action(item: dict, allowed: set[int], *, limit: int = 8) -> str:
    """Validate LLM-cited PR numbers against the comment batch."""
    raw = item.get("prs")
    if not isinstance(raw, list):
        return ""
    out: list[str] = []
    for part in raw:
        try:
            n = int(part)
        except (TypeError, ValueError):
            continue
        if n <= 0 or n not in allowed or str(n) in out:
            continue
        out.append(str(n))
        if len(out) >= limit:
            break
    return ",".join(out)


async def synthesize_from_human_reviews(
    store: IndexStore,
    llm,  # type: ignore[no-untyped-def]
    *,
    on_progress=None,  # type: ignore[no-untyped-def]
    replace_pending: bool = False,
) -> int:
    """Staged human-pattern synth: extract → cluster → upsert.

    Collects ``human_review`` feedback (hunk-preferring sample), runs the
    extract/cluster pipeline, and applies create/merge actions.
    Approved/rejected rule text stays frozen.

    When ``replace_pending`` is True (admin Rebuild / backfill end), auto-synth
    pending rows are cleared first so Pending reflects the new run.

    Returns the number of rules created or updated (skips do not count).
    """
    from mira.analysis.human_synth import run_staged_human_synth

    if replace_pending:
        clear_pending_synth_rules(store)

    events = store.list_feedback(limit=_FEEDBACK_FETCH_LIMIT)
    recent = _select_human_comments_for_synth(events, _MAX_HUMAN_COMMENTS)
    if len(recent) < 2:
        return 0

    comments = []
    for e in recent:
        body, code = unpack_human_comment_for_synth(str(e.comment_title or ""))
        comments.append(
            {
                "path": e.comment_path,
                "line": e.comment_line,
                "author": e.actor,
                "body": body,
                "code": code,
                "pr_number": int(getattr(e, "pr_number", 0) or 0),
            }
        )
    allowed_prs = {c["pr_number"] for c in comments if c["pr_number"] > 0}
    catalog = _catalog_for_synth(store)
    rejected = _rejected_for_synth(store)
    catalog_rows = [
        row
        for row in store.list_learned_rules()
        if row.source_signal in ("human_pattern", "manual")
        and row.status in ("pending", "approved")
    ]
    rejected_rows = [
        row
        for row in store.list_learned_rules()
        if row.source_signal in ("human_pattern", "manual") and row.status == "rejected"
    ]

    actions = await run_staged_human_synth(
        llm,
        comments=comments,
        catalog=catalog,
        max_rules=_MAX_LLM_RULES,
        on_progress=on_progress,
        rejected=rejected,
    )
    if not actions:
        return 0

    by_text = _existing_human_patterns_by_text(store)
    seen_batch: set[str] = set()
    upserted = 0
    for item in actions[:_MAX_LLM_RULES]:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower()
        if action == "skip":
            continue
        prs = _prs_csv_from_action(item, allowed_prs)
        rule_text = rule_text_from_synth_action(item)
        path_hint = sanitize_path_hint(str(item.get("path_hint") or ""))
        if action == "merge":
            try:
                target_id = int(item.get("target_id"))
            except (TypeError, ValueError):
                continue
            evidence = int(item.get("evidence_count") or 0)
            if _apply_merge(
                store,
                target_id=target_id,
                rule_text=rule_text,
                evidence=evidence,
                by_text=by_text,
                evidence_prs=prs,
            ):
                upserted += 1
            continue
        if action != "create":
            continue
        if not rule_text:
            continue
        evidence = int(item.get("evidence_count") or 0)
        if _apply_create(
            store,
            rule_text=rule_text,
            evidence=evidence,
            by_text=by_text,
            seen_batch=seen_batch,
            evidence_prs=prs,
            catalog_rows=catalog_rows,
            path_hint=path_hint,
            rejected_rows=rejected_rows,
        ):
            upserted += 1

    logger.info("Human synth upserted=%d", upserted)
    if on_progress:
        try:
            on_progress({"phase": "complete", "llm_rules": upserted})
        except Exception:
            logger.debug("Human synth progress callback failed", exc_info=True)
    return upserted
