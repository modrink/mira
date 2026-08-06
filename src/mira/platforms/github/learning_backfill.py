"""Backfill historical learnings from merged PR review comments.

Same shape as ``contributor_backfill`` (progress blobs, rate-limit floor,
sequential repos, ``backfill_repo_*`` / ``backfill_all_repos``) but reuses
``run_pr_merged_learning`` for ingest. Synthesis runs once per repo at end.

GitHub-only (same scope as contributor/review backfill).

Repos run **sequentially** on purpose: one installation token shares GitHub
secondary rate limits; parallel multi-repo backfills burn the budget and
trip sleeps. Wall-clock win is small vs 403/retry pain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC
from typing import Any

from github import Github, GithubException

from mira.platforms.github.auth import GitHubAppAuth

logger = logging.getLogger(__name__)

_RATE_LIMIT_FLOOR = 200
# Persist progress every N PRs during ingest. 1 = smooth UI; was 10 (jumpy).
_PROGRESS_EVERY = 1
_STATUS_PREFIX = "learning_backfill:"
DEFAULT_MAX_PRS = 100


def _status_key(owner: str, repo: str) -> str:
    return f"{_STATUS_PREFIX}{owner}/{repo}"


def get_backfill_status(db: Any, owner: str, repo: str) -> dict[str, Any]:
    """Read the persisted progress blob for a repo's learning backfill, or {}."""
    raw = db.get_setting(_status_key(owner, repo))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _set_status(db: Any, owner: str, repo: str, **fields: Any) -> None:
    """Merge ``fields`` into the repo's progress blob (survives restarts)."""
    data = get_backfill_status(db, owner, repo)
    data.update(fields)
    data["updated_at"] = time.time()
    db.set_setting(_status_key(owner, repo), json.dumps(data))


def mark_repos_running(
    db: Any, repos: list[tuple[str, str]], *, max_prs: int = DEFAULT_MAX_PRS
) -> None:
    """Queue selected repos so UI polls don't treat a prior complete as done.

    Only the worker flips a repo to ``running`` when it actually starts — avoids
    every row showing ``0/0`` while earlier repos still list/ingest.
    """
    for owner, repo in repos:
        _set_status(
            db,
            owner,
            repo,
            status="queued",
            error="",
            job="backfill",
            phase="queued",
            prs_done=0,
            total=0,
            max_prs=max(1, max_prs),
            skipped=0,
        )


def mark_repos_synth(db: Any, repos: list[tuple[str, str]]) -> None:
    """Queue repos for admin Rebuild learnings (synth-only, no GitHub fetch)."""
    for owner, repo in repos:
        _set_status(
            db,
            owner,
            repo,
            status="queued",
            error="",
            job="synth",
            phase="queued",
            extract_done=0,
            extract_total=0,
            deterministic_rules=0,
            llm_rules=0,
        )


def repos_with_active_job(db: Any, repos: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return ``(owner, repo)`` pairs that are already queued or running."""
    active: list[tuple[str, str]] = []
    for owner, repo in repos:
        st = get_backfill_status(db, owner, repo)
        if st.get("status") in ("queued", "running"):
            active.append((owner, repo))
    return active


def _dt_to_epoch(value: Any) -> float:
    """PyGithub datetime (naive or aware UTC) → epoch seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    except Exception:
        return 0.0


def _maybe_wait_for_rate_limit(gh: Github) -> None:
    """Sleep until reset if the core API budget is nearly exhausted.

    PyGithub >=2.7 returns ``RateLimitOverview``; core REST budget is
    ``.resources.core`` (was ``.core`` on the old ``RateLimit`` return type).
    """
    try:
        core = gh.get_rate_limit().resources.core
    except GithubException:
        return
    if core.remaining >= _RATE_LIMIT_FLOOR:
        return
    wait = max(0.0, _dt_to_epoch(core.reset) - time.time()) + 5
    wait = min(wait, 3600)
    logger.warning(
        "GitHub rate limit low (%s remaining); sleeping %.0fs until reset",
        core.remaining,
        wait,
    )
    time.sleep(wait)


def _list_merged_prs_sync(
    token: str,
    owner: str,
    repo: str,
    *,
    since: float | None,
    max_prs: int,
) -> list[dict[str, Any]]:
    """Blocking: collect up to ``max_prs`` merged PRs (newest updated first)."""
    gh = Github(token)
    gh_repo = gh.get_repo(f"{owner}/{repo}")
    pulls = gh_repo.get_pulls(state="closed", sort="updated", direction="desc")
    out: list[dict[str, Any]] = []
    scanned = 0
    for pr in pulls:
        scanned += 1
        if scanned % 10 == 0:
            _maybe_wait_for_rate_limit(gh)
        if not pr.merged_at:
            continue
        # Sorted by *updated*, not merged_at — skip old merges, don't break.
        if since and _dt_to_epoch(pr.merged_at) < since:
            continue
        user = pr.user
        merged_by_user = pr.merged_by
        out.append(
            {
                "number": pr.number,
                "title": pr.title or "",
                "url": pr.html_url or f"https://github.com/{owner}/{repo}/pull/{pr.number}",
                "author": (user.login or "") if user else "",
                "merged_by": (merged_by_user.login or "") if merged_by_user else "",
                "base_branch": pr.base.ref if pr.base else "",
                "head_branch": pr.head.ref if pr.head else "",
                "head_sha": (pr.head.sha or "") if pr.head else "",
                "body": pr.body or "",
            }
        )
        if len(out) >= max_prs:
            break
    return out


async def backfill_repo_learnings(
    owner: str,
    repo: str,
    app_auth: GitHubAppAuth,
    *,
    installation_id: int = 0,
    since: float | None = None,
    max_prs: int = DEFAULT_MAX_PRS,
    bot_name: str | None = None,
) -> dict[str, int]:
    """Backfill learnings for one repo from recent merged PRs. Returns counts."""
    from mira.dashboard.api import _app_db
    from mira.models import PRInfo
    from mira.platforms.handlers import run_pr_merged_learning, synthesize_repo_learnings
    from mira.providers import create_provider

    db = _app_db
    if not installation_id:
        rec = db.get_repo(owner, repo)
        installation_id = rec.installation_id if rec else 0
    if bot_name is None:
        bot_name = db.get_setting("bot_name") or "miracodeai"

    max_prs = max(1, max_prs)
    counts = {
        "prs": 0,
        "skipped": 0,
        "accepted": 0,
        "human_recorded": 0,
        "deterministic_rules": 0,
        "llm_rules": 0,
    }
    _set_status(
        db,
        owner,
        repo,
        status="running",
        error="",
        job="backfill",
        phase="listing",
        prs_done=0,
        total=0,
        max_prs=max_prs,
        skipped=0,
        accepted=0,
        human_recorded=0,
    )
    try:
        token = await app_auth.get_installation_token(installation_id)
        provider = create_provider("github", token)

        merged = await asyncio.to_thread(
            _list_merged_prs_sync,
            token,
            owner,
            repo,
            since=since,
            max_prs=max_prs,
        )
        total = len(merged)
        _set_status(
            db,
            owner,
            repo,
            phase="ingest" if total else "synth",
            total=total,
            max_prs=max_prs,
            prs_done=0,
        )

        for i, meta in enumerate(merged, start=1):
            pr_info = PRInfo(
                title=meta["title"],
                description=meta["body"],
                base_branch=meta["base_branch"],
                head_branch=meta["head_branch"],
                url=meta["url"],
                number=meta["number"],
                owner=owner,
                repo=repo,
                head_sha=meta["head_sha"],
                author=meta["author"],
            )
            result = await run_pr_merged_learning(
                provider,
                pr_info,
                bot_name,
                meta["merged_by"],
                platform="github",
                synthesize=False,
            )
            counts["prs"] += 1
            counts["skipped"] += result.get("skipped", 0)
            counts["accepted"] += result.get("accepted", 0)
            counts["human_recorded"] += result.get("human_recorded", 0)
            if i % _PROGRESS_EVERY == 0 or i == total:
                _set_status(
                    db,
                    owner,
                    repo,
                    phase="ingest",
                    prs_done=i,
                    total=total,
                    max_prs=max_prs,
                    skipped=counts["skipped"],
                    accepted=counts["accepted"],
                    human_recorded=counts["human_recorded"],
                )

        _set_status(db, owner, repo, job="backfill", phase="synth", prs_done=total, total=total)

        def on_progress(fields: dict[str, Any]) -> None:
            _set_status(db, owner, repo, job="backfill", status="running", **fields)

        synth = await synthesize_repo_learnings(
            owner, repo, platform="github", on_progress=on_progress
        )
        counts["deterministic_rules"] = synth.get("deterministic_rules", 0)
        counts["llm_rules"] = synth.get("llm_rules", 0)
    except Exception as exc:
        logger.exception("Learning backfill failed for %s/%s", owner, repo)
        _set_status(
            db,
            owner,
            repo,
            status="failed",
            phase="failed",
            job="backfill",
            error=str(exc),
            **counts,
        )
        raise
    _set_status(
        db,
        owner,
        repo,
        status="complete",
        phase="complete",
        job="backfill",
        prs_done=counts["prs"],
        total=counts["prs"],
        max_prs=max_prs,
        **counts,
    )
    logger.info("Learning backfill %s/%s: %s", owner, repo, counts)
    return counts


async def synthesize_repo_with_progress(
    owner: str,
    repo: str,
    *,
    platform: str = "github",
) -> dict[str, int]:
    """Admin Rebuild learnings for one repo; writes ``job=synth`` progress blobs."""
    from mira.dashboard.api import _app_db
    from mira.platforms.handlers import synthesize_repo_learnings

    db = _app_db

    def on_progress(fields: dict[str, Any]) -> None:
        _set_status(db, owner, repo, job="synth", status="running", **fields)

    _set_status(
        db,
        owner,
        repo,
        status="running",
        error="",
        job="synth",
        phase="extract",
        extract_done=0,
        extract_total=0,
    )
    try:
        counts = await synthesize_repo_learnings(
            owner, repo, platform=platform, on_progress=on_progress
        )
    except Exception as exc:
        logger.exception("Learnings synth failed for %s/%s", owner, repo)
        _set_status(
            db,
            owner,
            repo,
            status="failed",
            phase="failed",
            job="synth",
            error=str(exc),
        )
        raise
    _set_status(
        db,
        owner,
        repo,
        status="complete",
        phase="complete",
        job="synth",
        **counts,
    )
    logger.info("Learnings synth %s/%s: %s", owner, repo, counts)
    return counts


async def synthesize_all_repos_with_progress(
    repos: list[tuple[str, str, str]],
) -> dict[str, int]:
    """Sequential Rebuild learnings for ``(owner, repo, platform)`` tuples."""
    totals = {"deterministic_rules": 0, "llm_rules": 0, "repos": 0}
    for owner, repo, platform in repos:
        try:
            counts = await synthesize_repo_with_progress(owner, repo, platform=platform or "github")
            totals["deterministic_rules"] += counts.get("deterministic_rules", 0)
            totals["llm_rules"] += counts.get("llm_rules", 0)
            totals["repos"] += 1
        except Exception:
            logger.exception("Learnings synth failed for %s/%s, continuing", owner, repo)
    return totals


async def backfill_all_repos(
    app_auth: GitHubAppAuth,
    *,
    repos: list[tuple[str, str]] | None = None,
    since: float | None = None,
    max_prs: int = DEFAULT_MAX_PRS,
) -> dict[str, int]:
    """Backfill learnings for ``repos``, or every registered GitHub repo.

    Sequential across repos (shared GitHub rate limit). See module docstring.
    """
    from mira.dashboard.api import _app_db

    totals = {
        "prs": 0,
        "skipped": 0,
        "accepted": 0,
        "human_recorded": 0,
        "deterministic_rules": 0,
        "llm_rules": 0,
        "repos": 0,
    }
    if repos is None:
        targets = [
            (rec.owner, rec.repo, rec.installation_id)
            for rec in _app_db.list_repos()
            if getattr(rec, "platform", "github") == "github"
        ]
    else:
        targets = []
        for owner, repo in repos:
            rec = _app_db.get_repo(owner, repo)
            targets.append((owner, repo, rec.installation_id if rec else 0))

    for owner, repo, installation_id in targets:
        try:
            counts = await backfill_repo_learnings(
                owner,
                repo,
                app_auth,
                installation_id=installation_id,
                since=since,
                max_prs=max_prs,
            )
            for key in (
                "prs",
                "skipped",
                "accepted",
                "human_recorded",
                "deterministic_rules",
                "llm_rules",
            ):
                totals[key] += counts.get(key, 0)
            totals["repos"] += 1
        except Exception:
            logger.exception("Learning backfill failed for %s/%s, continuing", owner, repo)
    return totals
