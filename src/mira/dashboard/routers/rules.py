"""Dashboard rules routes"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from mira.dashboard import api as _api
from mira.dashboard.api import (
    LearnedRuleActiveInput,
    LearnedRuleInput,
    LearnedRuleModel,
    OrgLearnedRuleModel,
    ReviewContextCreate,
    ReviewContextModel,
    RuleCreate,
    RuleModel,
    _build_app_auth,
    _open_store,
    _pick_platform_record,
    _require_admin,
    router,
)

PlatformQ = Annotated[str | None, Query()]


@router.get("/api/repos/{owner}/{repo}/context", response_model=list[ReviewContextModel])
def list_context(owner: str, repo: str) -> list[ReviewContextModel]:
    with _open_store(owner, repo) as store:
        entries = store.list_review_context()
        return [
            ReviewContextModel(
                id=e.id,
                title=e.title,
                content=e.content,
                created_at=e.created_at,
                updated_at=e.updated_at,
            )
            for e in entries
        ]


@router.post("/api/repos/{owner}/{repo}/context", response_model=ReviewContextModel)
def create_context(owner: str, repo: str, body: ReviewContextCreate) -> ReviewContextModel:
    with _open_store(owner, repo) as store:
        e = store.upsert_review_context(title=body.title, content=body.content)
        return ReviewContextModel(
            id=e.id,
            title=e.title,
            content=e.content,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.put("/api/repos/{owner}/{repo}/context/{context_id}", response_model=ReviewContextModel)
def update_context(
    owner: str, repo: str, context_id: int, body: ReviewContextCreate
) -> ReviewContextModel:
    with _open_store(owner, repo) as store:
        existing = store.get_review_context(context_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Context not found")
        e = store.upsert_review_context(
            title=body.title, content=body.content, context_id=context_id
        )
        return ReviewContextModel(
            id=e.id,
            title=e.title,
            content=e.content,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.delete("/api/repos/{owner}/{repo}/context/{context_id}")
def delete_context(owner: str, repo: str, context_id: int) -> dict:
    with _open_store(owner, repo) as store:
        store.delete_review_context(context_id)
        return {"ok": True}


@router.get(
    "/api/repos/{owner}/{repo}/learned-rules",
    response_model=list[LearnedRuleModel],
)
def list_repo_learned_rules(owner: str, repo: str) -> list[LearnedRuleModel]:
    """Active learned rules synthesized from feedback signals on this repo."""
    with _open_store(owner, repo) as store:
        rules = store.list_active_learned_rules()
        return [
            LearnedRuleModel(
                id=r.id,
                rule_text=r.rule_text,
                source_signal=r.source_signal,
                category=r.category,
                path_pattern=r.path_pattern,
                sample_count=r.sample_count,
                active=r.active,
                status=r.status,
                created_by=r.created_by,
                evidence_prs=getattr(r, "evidence_prs", "") or "",
                updated_at=r.updated_at,
            )
            for r in rules
        ]


@router.get("/api/learned-rules", response_model=list[OrgLearnedRuleModel])
def list_org_learned_rules(limit: int = 500, status: str = "") -> list[OrgLearnedRuleModel]:
    """Learned rules across every repo in the org.

    `status` filters by approval state ('pending'|'approved'|'rejected');
    empty returns all so admins can manage the full set.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    capped = max(1, min(limit, 2000))
    status_filter = status or None
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import list_learned_rules_org_wide

        rows = list_learned_rules_org_wide(db_url, limit=capped, status=status_filter)
    else:
        from mira.index.store import list_learned_rules_org_wide_sqlite

        rows = list_learned_rules_org_wide_sqlite(limit=capped, status=status_filter)
    return [
        OrgLearnedRuleModel(
            id=r.get("id", 0),
            owner=r["owner"],
            repo=r["repo"],
            platform=r.get("platform", "github") or "github",
            rule_text=r["rule_text"],
            source_signal=r["source_signal"],
            category=r["category"],
            path_pattern=r["path_pattern"],
            sample_count=r["sample_count"],
            active=r.get("active", True),
            status=r.get("status", "approved"),
            created_by=r.get("created_by", ""),
            evidence_prs=r.get("evidence_prs", "") or "",
            group_id=r.get("group_id", "") or "",
            updated_at=r["updated_at"] or 0.0,
        )
        for r in rows
    ]


# ── Learnings approval queue + CRUD (admin only) ───────────────────────────
# Auto-synthesized learnings land 'pending' and must be approved by an admin
# before they influence reviews. Admins can also author/edit/delete rules.


@router.get(
    "/api/learned-rules/{owner}/{repo}/{rule_id}",
    response_model=OrgLearnedRuleModel,
)
def get_learned_rule_detail(
    owner: str,
    repo: str,
    rule_id: int,
    request: Request,
    platform: PlatformQ = None,
) -> OrgLearnedRuleModel:
    """Single learned rule — backs the edit page. Readable by any authenticated
    user (so a creator can load their own pending rule to edit)."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    username = getattr(user, "username", "") if user else ""
    with _open_store(owner, repo, platform=platform) as store:
        r = store.get_learned_rule(rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")
    if r.status != "approved" and not is_admin and r.created_by != username:
        raise HTTPException(status_code=403, detail="Not allowed to view this rule")
    if platform:
        resolved = platform
    else:
        matches = _api._app_db.get_repo_any_platform(owner, repo)
        resolved = (
            getattr(_pick_platform_record(matches), "platform", "github") if matches else "github"
        )
    return OrgLearnedRuleModel(
        id=r.id,
        owner=owner,
        repo=repo,
        platform=resolved or "github",
        rule_text=r.rule_text,
        source_signal=r.source_signal,
        category=r.category,
        path_pattern=r.path_pattern,
        sample_count=r.sample_count,
        active=r.active,
        status=r.status,
        created_by=r.created_by,
        evidence_prs=getattr(r, "evidence_prs", "") or "",
        group_id=getattr(r, "group_id", "") or "",
        updated_at=r.updated_at,
    )


@router.post("/api/learned-rules/{owner}/{repo}/{rule_id}/approve")
def approve_learned_rule(
    owner: str,
    repo: str,
    rule_id: int,
    request: Request,
    platform: PlatformQ = None,
) -> dict:
    _require_admin(request)
    with _open_store(owner, repo, platform=platform) as store:
        store.set_learned_rule_status(rule_id, "approved")
    return {"ok": True}


@router.post("/api/learned-rules/{owner}/{repo}/{rule_id}/reject")
def reject_learned_rule(
    owner: str,
    repo: str,
    rule_id: int,
    request: Request,
    platform: PlatformQ = None,
) -> dict:
    _require_admin(request)
    with _open_store(owner, repo, platform=platform) as store:
        store.set_learned_rule_status(rule_id, "rejected")
    return {"ok": True}


@router.patch("/api/learned-rules/{owner}/{repo}/{rule_id}/active")
def set_learned_rule_active(
    owner: str,
    repo: str,
    rule_id: int,
    body: LearnedRuleActiveInput,
    request: Request,
    platform: PlatformQ = None,
) -> dict:
    _require_admin(request)
    with _open_store(owner, repo, platform=platform) as store:
        store.set_learned_rule_active(rule_id, body.active)
    return {"ok": True}


@router.post("/api/learned-rules/{owner}/{repo}", response_model=LearnedRuleModel)
def create_learned_rule(
    owner: str,
    repo: str,
    body: LearnedRuleInput,
    request: Request,
    platform: PlatformQ = None,
) -> LearnedRuleModel:
    # Anyone authenticated may author a learning; admins' land approved, while
    # everyone else's go to the pending queue for an admin to approve.
    # Near-duplicates reinforce an existing row (same as @remember).
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    from mira.analysis.learned_rules import find_near_duplicate_rule

    with _open_store(owner, repo, platform=platform) as store:
        catalog = [row for row in store.list_learned_rules() if row.status != "rejected"]
        near = find_near_duplicate_rule(body.rule_text, catalog)
        if near is not None:
            bumped = store.bump_learned_rule_evidence(near.id, max(1, near.sample_count) + 1)
            r = bumped or near
        else:
            r = store.create_learned_rule(
                rule_text=body.rule_text,
                category=body.category,
                path_pattern=body.path_pattern,
                status="approved" if is_admin else "pending",
                created_by=getattr(user, "username", "") if user else "",
            )
    return LearnedRuleModel(
        id=r.id,
        rule_text=r.rule_text,
        source_signal=r.source_signal,
        category=r.category,
        path_pattern=r.path_pattern,
        sample_count=r.sample_count,
        active=r.active,
        status=r.status,
        created_by=r.created_by,
        evidence_prs=getattr(r, "evidence_prs", "") or "",
        updated_at=r.updated_at,
    )


@router.put("/api/learned-rules/{owner}/{repo}/{rule_id}")
def update_learned_rule(
    owner: str,
    repo: str,
    rule_id: int,
    body: LearnedRuleInput,
    request: Request,
    platform: PlatformQ = None,
) -> dict:
    # Admins may edit any rule; a non-admin may edit only their own rule while
    # it's still pending approval.
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    username = getattr(user, "username", "") if user else ""
    with _open_store(owner, repo, platform=platform) as store:
        existing = store.get_learned_rule(rule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Rule not found")
        if not (is_admin or (existing.created_by == username and existing.status == "pending")):
            raise HTTPException(status_code=403, detail="Not allowed to edit this rule")
        store.update_learned_rule(rule_id, body.rule_text, body.category, body.path_pattern)
    return {"ok": True}


@router.delete("/api/learned-rules/{owner}/{repo}/{rule_id}")
def delete_learned_rule(
    owner: str,
    repo: str,
    rule_id: int,
    request: Request,
    platform: PlatformQ = None,
) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    username = getattr(user, "username", "") if user else ""
    with _open_store(owner, repo, platform=platform) as store:
        existing = store.get_learned_rule(rule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Rule not found")
        if not (is_admin or (existing.created_by == username and existing.status == "pending")):
            raise HTTPException(status_code=403, detail="Not allowed to delete this rule")
        store.delete_learned_rule(rule_id)
    return {"ok": True}


@router.get("/api/repos/{owner}/{repo}/rules", response_model=list[RuleModel])
def list_repo_rules(owner: str, repo: str) -> list[RuleModel]:
    with _open_store(owner, repo) as store:
        entries = store.list_review_context()
        return [
            RuleModel(
                id=e.id,
                title=e.title,
                content=e.content,
                enabled=True,
                created_at=e.created_at,
                updated_at=e.updated_at,
            )
            for e in entries
        ]


@router.post("/api/repos/{owner}/{repo}/rules", response_model=RuleModel)
def create_repo_rule(owner: str, repo: str, body: RuleCreate) -> RuleModel:
    with _open_store(owner, repo) as store:
        e = store.upsert_review_context(title=body.title, content=body.content)
        return RuleModel(
            id=e.id,
            title=e.title,
            content=e.content,
            enabled=True,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.put("/api/repos/{owner}/{repo}/rules/{rule_id}", response_model=RuleModel)
def update_repo_rule(owner: str, repo: str, rule_id: int, body: RuleCreate) -> RuleModel:
    with _open_store(owner, repo) as store:
        existing = store.get_review_context(rule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Rule not found")
        e = store.upsert_review_context(title=body.title, content=body.content, context_id=rule_id)
        return RuleModel(
            id=e.id,
            title=e.title,
            content=e.content,
            enabled=True,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.delete("/api/repos/{owner}/{repo}/rules/{rule_id}")
def delete_repo_rule(owner: str, repo: str, rule_id: int) -> dict:
    with _open_store(owner, repo) as store:
        store.delete_review_context(rule_id)
        return {"ok": True}


@router.get("/api/rules/global", response_model=list[RuleModel])
def list_global_rules() -> list[RuleModel]:
    rules = _api._app_db.list_global_rules()
    return [
        RuleModel(
            id=r.id,
            title=r.title,
            content=r.content,
            enabled=r.enabled,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rules
    ]


@router.post("/api/rules/global", response_model=RuleModel)
def create_global_rule(body: RuleCreate) -> RuleModel:
    r = _api._app_db.upsert_global_rule(title=body.title, content=body.content)
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.put("/api/rules/global/{rule_id}", response_model=RuleModel)
def update_global_rule(rule_id: int, body: RuleCreate) -> RuleModel:
    existing = _api._app_db.get_global_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    r = _api._app_db.upsert_global_rule(title=body.title, content=body.content, rule_id=rule_id)
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.delete("/api/rules/global/{rule_id}")
def delete_global_rule(rule_id: int) -> dict:
    _api._app_db.delete_global_rule(rule_id)
    return {"ok": True}


@router.patch("/api/rules/global/{rule_id}/toggle", response_model=RuleModel)
def toggle_global_rule(rule_id: int) -> RuleModel:
    r = _api._app_db.toggle_global_rule(rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


# ── Learnings history backfill (admin) — mirrors contributors refresh ──────


class _LearningsRepoRef(BaseModel):
    owner: str
    repo: str


class LearningsBackfillRequest(BaseModel):
    repos: list[_LearningsRepoRef] | None = None
    max_prs: int = Field(default=100, ge=1, le=5000)


def _github_repo_tuples(
    explicit: list[_LearningsRepoRef] | None,
) -> list[tuple[str, str]]:
    if explicit is not None:
        return [(r.owner, r.repo) for r in explicit]
    return [
        (rec.owner, rec.repo)
        for rec in _api._app_db.list_repos()
        if getattr(rec, "platform", "github") == "github"
    ]


@router.get("/api/learnings/backfill/status")
def learnings_backfill_status(request: Request) -> list[dict]:
    """Per-repo learning-backfill progress blobs. Admin only."""
    _require_admin(request)
    from mira.platforms.github.learning_backfill import get_backfill_status

    out: list[dict] = []
    for rec in _api._app_db.list_repos():
        if getattr(rec, "platform", "github") != "github":
            continue
        status = get_backfill_status(_api._app_db, rec.owner, rec.repo)
        if status:
            out.append({"owner": rec.owner, "repo": rec.repo, **status})
    return out


@router.get("/api/learnings/backfill/estimate")
def learnings_backfill_estimate(
    request: Request,
    repos: int = 0,
    max_prs: int = 100,
    repo: Annotated[list[str] | None, Query()] = None,
) -> dict:
    """LLM cost for learnings backfill synth calls.

    Prefer ``repo=owner/name`` (repeatable) + ``max_prs`` — uses stored
    human_review + catalog sizes. Legacy ``repos=N`` keeps a flat worst-case.
    """
    _require_admin(request)
    from mira.config import load_config
    from mira.dashboard.models_config import (
        MODEL_PRICING,
        estimate_learnings_backfill_cost,
        estimate_repo_synth_tokens,
        get_indexing_model,
    )
    from mira.platforms.github.learning_backfill import get_backfill_status

    config = load_config()
    model = get_indexing_model(config.llm, _api._app_db.get_setting("indexing_model"))
    max_prs = max(1, min(5000, max_prs))

    repo_keys = [r.strip() for r in (repo or []) if r and "/" in r.strip()]
    if repo_keys:
        in_tokens = 0
        out_tokens = 0
        synth_calls = 0
        skipped = 0
        for key in repo_keys:
            owner, name = key.split("/", 1)
            humans = 0
            catalog = 0
            try:
                with _open_store(owner, name, platform="github") as store:
                    for e in store.list_feedback(limit=2000):
                        if getattr(e, "signal", "") == "human_review":
                            humans += 1
                    for row in store.list_learned_rules():
                        if row.source_signal in ("human_pattern", "manual") and row.status in (
                            "pending",
                            "approved",
                        ):
                            catalog += 1
            except Exception:
                prev = get_backfill_status(_api._app_db, owner, name)
                humans = int(prev.get("human_recorded") or 0)
            tok = estimate_repo_synth_tokens(
                human_review_count=humans,
                catalog_count=catalog,
                max_prs=max_prs,
            )
            if tok is None:
                skipped += 1
                continue
            synth_calls += 1
            in_tokens += tok[0]
            out_tokens += tok[1]

        input_price, output_price = MODEL_PRICING.get(model, (3.00, 15.00))
        cost = (in_tokens / 1_000_000) * input_price + (out_tokens / 1_000_000) * output_price
        return {
            "estimated_usd": round(cost, 2),
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "repo_count": len(repo_keys),
            "synth_calls": synth_calls,
            "skipped_repos": skipped,
            "max_prs": max_prs,
            "model": model,
            "basis": "stored_feedback_and_catalog",
        }

    n = max(0, repos)
    est = estimate_learnings_backfill_cost(n, model)
    return {
        "estimated_usd": est["estimated_usd"],
        "input_tokens": est["input_tokens"],
        "output_tokens": est["output_tokens"],
        "repo_count": n,
        "synth_calls": est.get("synth_calls", n),
        "skipped_repos": 0,
        "max_prs": max_prs,
        "model": model,
        "basis": "flat_per_repo",
    }


@router.post("/api/learnings/refresh")
async def refresh_learnings(request: Request, body: LearningsBackfillRequest | None = None) -> dict:
    """Kick off a background learnings backfill (admin only).

    Omit ``repos`` to scan every registered GitHub repo. Default ``max_prs`` is 100.
    """
    _require_admin(request)
    auth = _build_app_auth()
    from mira.platforms.github.learning_backfill import (
        backfill_all_repos,
        mark_repos_running,
    )

    payload = body or LearningsBackfillRequest()
    repo_tuples = _github_repo_tuples(payload.repos)
    mark_repos_running(_api._app_db, repo_tuples, max_prs=payload.max_prs)
    asyncio.create_task(backfill_all_repos(auth, repos=repo_tuples, max_prs=payload.max_prs))
    return {"status": "refreshing"}


@router.post("/api/learnings/{owner}/{repo}/refresh")
async def refresh_learnings_repo(
    owner: str,
    repo: str,
    request: Request,
    max_prs: int = 100,
) -> dict:
    """Kick off a background learnings backfill for one repo (admin only)."""
    _require_admin(request)
    rec = _api._app_db.get_repo(owner, repo, platform="github")
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{repo} not found")
    auth = _build_app_auth()
    from mira.platforms.github.learning_backfill import (
        backfill_repo_learnings,
        mark_repos_running,
    )

    mark_repos_running(_api._app_db, [(owner, repo)], max_prs=max(1, min(max_prs, 5000)))
    asyncio.create_task(
        backfill_repo_learnings(
            owner,
            repo,
            auth,
            installation_id=rec.installation_id,
            max_prs=max(1, min(max_prs, 5000)),
        )
    )
    return {"status": "refreshing"}


@router.post("/api/learnings/{owner}/{repo}/synthesize")
async def synthesize_learnings_repo(owner: str, repo: str, request: Request) -> dict:
    """Kick off catalog-aware learnings synthesis for one repo (no GitHub fetch).

    Returns immediately; progress is polled via ``/api/learnings/backfill/status``
    (``job=synth`` blobs).
    """
    _require_admin(request)
    matches = _api._app_db.get_repo_any_platform(owner, repo)
    if not matches:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{repo} not found")
    platform = getattr(_api._pick_platform_record(matches), "platform", "github") or "github"
    from mira.platforms.github.learning_backfill import (
        mark_repos_synth,
        repos_with_active_job,
        synthesize_repo_with_progress,
    )

    busy = repos_with_active_job(_api._app_db, [(owner, repo)])
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"Learnings job already running for {owner}/{repo}",
        )
    mark_repos_synth(_api._app_db, [(owner, repo)])
    asyncio.create_task(synthesize_repo_with_progress(owner, repo, platform=platform))
    return {"status": "refreshing"}


class LearningsSynthesizeRequest(BaseModel):
    repos: list[_LearningsRepoRef] | None = None


@router.post("/api/learnings/synthesize")
async def synthesize_learnings(
    request: Request, body: LearningsSynthesizeRequest | None = None
) -> dict:
    """Kick off learnings synthesis for selected repos (or every registered repo).

    Returns immediately; progress via ``/api/learnings/backfill/status``.
    """
    _require_admin(request)
    from mira.platforms.github.learning_backfill import (
        mark_repos_synth,
        repos_with_active_job,
        synthesize_all_repos_with_progress,
    )

    payload = body or LearningsSynthesizeRequest()
    targets: list[tuple[str, str, str]] = []
    if payload.repos is not None:
        for ref in payload.repos:
            matches = _api._app_db.get_repo_any_platform(ref.owner, ref.repo)
            if not matches:
                continue
            platform = (
                getattr(_api._pick_platform_record(matches), "platform", "github") or "github"
            )
            targets.append((ref.owner, ref.repo, platform))
    else:
        for rec in _api._app_db.list_repos():
            platform = getattr(rec, "platform", "github") or "github"
            targets.append((rec.owner, rec.repo, platform))

    if not targets:
        return {"status": "refreshing", "repos": 0}

    pairs = [(o, r) for o, r, _ in targets]
    busy = repos_with_active_job(_api._app_db, pairs)
    if busy:
        labels = ", ".join(f"{o}/{r}" for o, r in busy[:5])
        more = f" (+{len(busy) - 5} more)" if len(busy) > 5 else ""
        raise HTTPException(
            status_code=409,
            detail=f"Learnings job already running for: {labels}{more}",
        )

    mark_repos_synth(_api._app_db, pairs)
    asyncio.create_task(synthesize_all_repos_with_progress(targets))
    return {"status": "refreshing", "repos": len(targets)}
