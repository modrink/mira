"""Unified Rules façade — Pending/Active product API over split stores."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import HTTPException, Query, Request

from mira.dashboard import api as _api
from mira.dashboard.api import (
    LearnedRuleActiveInput,
    LearnedRuleInput,
    RuleCreate,
    UnifiedRule,
    UnifiedRuleCreate,
    UnifiedRuleEnabled,
    UnifiedRuleRef,
    UnifiedRuleUpdate,
    _open_store,
    router,
)
from mira.dashboard.routers import rules as legacy

ModeQ = Annotated[str, Query(description="pending | active (aliases: inbox | catalog)")]
KindQ = Annotated[str | None, Query()]
RepoQ = Annotated[str | None, Query(description="owner/repo filter")]
EnabledQ = Annotated[str | None, Query(description="true|false|all")]
SearchQ = Annotated[str | None, Query(alias="q")]
LimitQ = Annotated[int, Query(ge=1, le=5000)]


def _from_global(r: object) -> UnifiedRule:
    title = str(getattr(r, "title", "") or "")
    content = str(getattr(r, "content", "") or "")
    return UnifiedRule(
        id=int(getattr(r, "id", 0)),
        kind="written_global",
        title=title,
        text=content,
        enabled=bool(getattr(r, "enabled", True)),
        status="approved",
        priority="written",
        updated_at=float(getattr(r, "updated_at", 0) or 0),
    )


def _from_repo_ctx(r: object, *, owner: str, repo: str, platform: str) -> UnifiedRule:
    title = str(getattr(r, "title", "") or "")
    content = str(getattr(r, "content", "") or "")
    return UnifiedRule(
        id=int(getattr(r, "id", 0)),
        kind="written_repo",
        owner=owner,
        repo=repo,
        platform=platform or "github",
        title=title,
        text=content,
        enabled=True,
        status="approved",
        priority="written",
        updated_at=float(getattr(r, "updated_at", 0) or 0),
    )


def _from_learned(r: dict | object) -> UnifiedRule:
    from mira.analysis.learned_rules import unpack_learned_rule

    if isinstance(r, dict):
        get = r.get
    else:

        def get(k: str, default: object = "") -> object:
            return getattr(r, k, default)

    raw = str(get("rule_text", "") or get("text", "") or "").strip()
    title, body = unpack_learned_rule(raw)
    text = body if title else raw
    owner = str(get("owner", "") or "")
    repo = str(get("repo", "") or "")
    return UnifiedRule(
        id=int(get("id", 0) or 0),
        kind="learned",
        owner=owner,
        repo=repo,
        platform=str(get("platform", "github") or "github"),
        title=title,
        text=text,
        enabled=bool(get("active", get("enabled", True))),
        status=str(get("status", "approved") or "approved"),
        category=str(get("category", "") or ""),
        path_pattern=str(get("path_pattern", "") or ""),
        source_signal=str(get("source_signal", "") or ""),
        sample_count=int(get("sample_count", 0) or 0),
        evidence_prs=str(get("evidence_prs", "") or ""),
        created_by=str(get("created_by", "") or ""),
        priority="learned",
        updated_at=float(get("updated_at", 0) or 0),
        group_id=str(get("group_id", "") or ""),
        repos=[f"{owner}/{repo}"] if owner and repo else [],
    )


def _list_org_learned(status: str | None) -> list[UnifiedRule]:
    from mira.dashboard.rule_scope import collapse_learned_rows

    db_url = os.environ.get("DATABASE_URL", "")
    capped = 2000
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import list_learned_rules_org_wide

        rows = list_learned_rules_org_wide(db_url, limit=capped, status=status)
    else:
        from mira.index.store import list_learned_rules_org_wide_sqlite

        rows = list_learned_rules_org_wide_sqlite(limit=capped, status=status)
    return collapse_learned_rows([_from_learned(r) for r in rows])


def _list_written_catalog() -> list[UnifiedRule]:
    out: list[UnifiedRule] = []
    for r in _api._app_db.list_global_rules():
        out.append(_from_global(r))
    for rec in _api._app_db.list_repos():
        platform = getattr(rec, "platform", "github") or "github"
        try:
            with _open_store(rec.owner, rec.repo, platform=platform) as store:
                for e in store.list_review_context():
                    row = _from_repo_ctx(e, owner=rec.owner, repo=rec.repo, platform=platform)
                    out.append(row.model_copy(update={"repos": [f"{rec.owner}/{rec.repo}"]}))
        except HTTPException:
            continue
        except Exception:
            continue
    return out


def _match_repo(row: UnifiedRule, repo_filter: str | None) -> bool:
    if not repo_filter or repo_filter in ("", "__all__"):
        return True
    if row.kind == "written_global":
        return False
    if row.repos:
        return repo_filter in row.repos
    return f"{row.owner}/{row.repo}" == repo_filter


def _match_kind(row: UnifiedRule, kind: str | None) -> bool:
    if not kind or kind == "all":
        return True
    return row.kind == kind


def _match_enabled(row: UnifiedRule, enabled: str | None) -> bool:
    if not enabled or enabled == "all":
        return True
    want = enabled.lower() in ("1", "true", "yes", "enabled")
    return row.enabled is want


def _match_q(row: UnifiedRule, q: str | None) -> bool:
    if not q or not q.strip():
        return True
    needle = q.strip().lower()
    blob = " ".join(
        [
            row.title,
            row.text,
            row.category,
            row.path_pattern,
            row.owner,
            row.repo,
            row.kind,
            row.source_signal,
        ]
    ).lower()
    return needle in blob


@router.get("/api/rules", response_model=list[UnifiedRule])
def list_unified_rules(
    mode: ModeQ = "active",
    kind: KindQ = None,
    repo: RepoQ = None,
    enabled: EnabledQ = None,
    q: SearchQ = None,
    limit: LimitQ = 2000,
) -> list[UnifiedRule]:
    """Pending (awaiting approval) or Active (written + approved learned)."""
    mode_norm = (mode or "active").strip().lower()
    # Aliases from earlier Inbox/Catalog naming.
    if mode_norm in ("inbox", "pending"):
        mode_norm = "pending"
    elif mode_norm in ("catalog", "active"):
        mode_norm = "active"
    else:
        raise HTTPException(status_code=400, detail="mode must be pending or active")

    if mode_norm == "pending":
        rows = _list_org_learned("pending")
        # Pending is learned-only; kind/enabled filters don't apply.
        filtered = [r for r in rows if _match_repo(r, repo) and _match_q(r, q)]
    else:
        rows = _list_written_catalog() + _list_org_learned("approved")
        filtered = [
            r
            for r in rows
            if _match_repo(r, repo)
            and _match_kind(r, kind)
            and _match_enabled(r, enabled)
            and _match_q(r, q)
        ]
    filtered.sort(key=lambda r: (-r.updated_at, r.kind, r.id))
    return filtered[:limit]


@router.get("/api/rules/item", response_model=UnifiedRule)
def get_unified_rule(
    kind: str,
    id: int,
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "github",
) -> UnifiedRule:
    """Single rule by composite key."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if kind == "written_global":
        r = _api._app_db.get_global_rule(id)
        if not r:
            raise HTTPException(status_code=404, detail="Rule not found")
        return _from_global(r)
    if kind == "written_repo":
        with _open_store(owner, repo, platform=platform or None) as store:
            e = store.get_review_context(id)
        if not e:
            raise HTTPException(status_code=404, detail="Rule not found")
        return _from_repo_ctx(e, owner=owner, repo=repo, platform=platform or "github")
    if kind == "learned":
        from mira.dashboard.rule_scope import attach_learned_repos

        detail = legacy.get_learned_rule_detail(owner, repo, id, request, platform=platform or None)
        return attach_learned_repos(
            _from_learned(
                {
                    "id": detail.id,
                    "owner": detail.owner,
                    "repo": detail.repo,
                    "platform": detail.platform,
                    "rule_text": detail.rule_text,
                    "active": detail.active,
                    "status": detail.status,
                    "category": detail.category,
                    "path_pattern": detail.path_pattern,
                    "source_signal": detail.source_signal,
                    "sample_count": detail.sample_count,
                    "evidence_prs": detail.evidence_prs,
                    "created_by": detail.created_by,
                    "updated_at": detail.updated_at,
                    "group_id": getattr(detail, "group_id", "") or "",
                }
            )
        )
    raise HTTPException(status_code=400, detail=f"Unknown kind {kind}")


@router.post("/api/rules", response_model=UnifiedRule)
def create_unified_rule(body: UnifiedRuleCreate, request: Request) -> UnifiedRule:
    if body.kind == "written_global":
        r = legacy.create_global_rule(RuleCreate(title=body.title or "Rule", content=body.text))
        return UnifiedRule(
            id=r.id,
            kind="written_global",
            title=r.title,
            text=r.content,
            enabled=r.enabled,
            status="approved",
            priority="written",
            updated_at=r.updated_at,
        )
    if body.kind == "written_repo":
        if not body.owner or not body.repo:
            raise HTTPException(status_code=400, detail="owner and repo required")
        created = legacy.create_repo_rule(
            body.owner,
            body.repo,
            RuleCreate(title=body.title or "Rule", content=body.text),
        )
        return _from_repo_ctx(
            created,
            owner=body.owner,
            repo=body.repo,
            platform=body.platform or "github",
        )
    if body.kind == "learned":
        if not body.owner or not body.repo:
            raise HTTPException(status_code=400, detail="owner and repo required")
        from mira.analysis.learned_rules import pack_learned_rule

        rule_text = pack_learned_rule(body.title, body.text)
        if not rule_text:
            raise HTTPException(status_code=400, detail="title and text required")
        created = legacy.create_learned_rule(
            body.owner,
            body.repo,
            LearnedRuleInput(
                rule_text=rule_text,
                category=body.category or "other",
                path_pattern=body.path_pattern,
            ),
            request,
            platform=body.platform or None,
        )
        return _from_learned(
            {
                "id": created.id,
                "owner": body.owner,
                "repo": body.repo,
                "platform": body.platform or "github",
                "rule_text": created.rule_text,
                "active": created.active,
                "status": created.status,
                "category": created.category,
                "path_pattern": created.path_pattern,
                "source_signal": created.source_signal,
                "sample_count": created.sample_count,
                "evidence_prs": getattr(created, "evidence_prs", "") or "",
                "created_by": created.created_by,
                "updated_at": created.updated_at,
            }
        )
    raise HTTPException(status_code=400, detail=f"Unknown kind {body.kind}")


@router.put("/api/rules", response_model=UnifiedRule)
def update_unified_rule(body: UnifiedRuleUpdate, request: Request) -> UnifiedRule:
    from mira.dashboard import rule_scope as scope

    scope_mode = (body.scope or "").strip().lower() or None

    if body.kind in ("written_global", "written_repo"):
        if scope_mode in ("global", "repos"):
            return scope.migrate_written_scope(
                kind=body.kind,
                rule_id=body.id,
                owner=body.owner,
                repo=body.repo,
                platform=body.platform or "github",
                title=body.title,
                text=body.text,
                scope=scope_mode,
                scope_repos=body.scope_repos,
            )
        if body.kind == "written_global":
            r = legacy.update_global_rule(
                body.id, RuleCreate(title=body.title or "Rule", content=body.text)
            )
            return UnifiedRule(
                id=r.id,
                kind="written_global",
                title=r.title,
                text=r.content,
                enabled=r.enabled,
                status="approved",
                priority="written",
                updated_at=r.updated_at,
            )
        r = legacy.update_repo_rule(
            body.owner,
            body.repo,
            body.id,
            RuleCreate(title=body.title or "Rule", content=body.text),
        )
        return _from_repo_ctx(
            r,
            owner=body.owner,
            repo=body.repo,
            platform=body.platform or "github",
        ).model_copy(update={"repos": [f"{body.owner}/{body.repo}"]})

    if body.kind == "learned":
        from mira.analysis.learned_rules import pack_learned_rule

        packed = pack_learned_rule(body.title, body.text)
        if not packed:
            raise HTTPException(status_code=400, detail="title and text required")
        if scope_mode == "global":
            return scope.promote_learned_to_global(
                owner=body.owner,
                repo=body.repo,
                platform=body.platform or "github",
                rule_id=body.id,
                text=packed,
                title=body.title,
            )
        if scope_mode == "repos" or body.scope_repos is not None:
            refs = body.scope_repos or []
            return scope.sync_learned_repos(
                owner=body.owner,
                repo=body.repo,
                platform=body.platform or "github",
                rule_id=body.id,
                text=packed,
                category=body.category or "other",
                path_pattern=body.path_pattern,
                scope_repos=refs,
            )
        # Text-only: sync all copies in the group.
        with _open_store(body.owner, body.repo, platform=body.platform or None) as store:
            existing = store.get_learned_rule(body.id)
        if not existing:
            raise HTTPException(status_code=404, detail="Rule not found")
        copies = scope.find_learned_copies(
            owner=body.owner,
            repo=body.repo,
            platform=body.platform or "github",
            rule_id=body.id,
            group_id=existing.group_id or "",
        )
        for c in copies or [
            {
                "owner": body.owner,
                "repo": body.repo,
                "platform": body.platform or "github",
                "id": body.id,
            }
        ]:
            with _open_store(
                str(c["owner"]),
                str(c["repo"]),
                platform=str(c.get("platform") or "github"),
            ) as store:
                store.update_learned_rule(
                    int(c["id"]),
                    packed,
                    body.category or "other",
                    body.path_pattern,
                )
        return get_unified_rule(
            "learned",
            body.id if not copies else int(copies[0]["id"]),
            request,
            owner=body.owner if not copies else str(copies[0]["owner"]),
            repo=body.repo if not copies else str(copies[0]["repo"]),
            platform=(body.platform if not copies else str(copies[0].get("platform") or "github")),
        )
    raise HTTPException(status_code=400, detail=f"Unknown kind {body.kind}")


@router.post("/api/rules/delete")
def delete_unified_rule(body: UnifiedRuleRef, request: Request) -> dict:
    if body.kind == "written_global":
        return legacy.delete_global_rule(body.id)
    if body.kind == "written_repo":
        return legacy.delete_repo_rule(body.owner, body.repo, body.id)
    if body.kind == "learned":
        from mira.dashboard import rule_scope as scope

        with _open_store(body.owner, body.repo, platform=body.platform or None) as store:
            existing = store.get_learned_rule(body.id)
        if not existing:
            raise HTTPException(status_code=404, detail="Rule not found")
        copies = scope.find_learned_copies(
            owner=body.owner,
            repo=body.repo,
            platform=body.platform or "github",
            rule_id=body.id,
            group_id=existing.group_id or "",
        )
        scope.fanout_learned_delete(
            copies
            or [
                {
                    "owner": body.owner,
                    "repo": body.repo,
                    "platform": body.platform or "github",
                    "id": body.id,
                }
            ]
        )
        return {"ok": True}
    raise HTTPException(status_code=400, detail=f"Unknown kind {body.kind}")


@router.post("/api/rules/approve")
def approve_unified_rule(body: UnifiedRuleRef, request: Request) -> dict:
    if body.kind != "learned":
        raise HTTPException(status_code=400, detail="Only learned rules can be approved")
    from mira.dashboard import rule_scope as scope

    with _open_store(body.owner, body.repo, platform=body.platform or None) as store:
        existing = store.get_learned_rule(body.id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    copies = scope.find_learned_copies(
        owner=body.owner,
        repo=body.repo,
        platform=body.platform or "github",
        rule_id=body.id,
        group_id=existing.group_id or "",
    )
    if len(copies) <= 1:
        return legacy.approve_learned_rule(
            body.owner, body.repo, body.id, request, platform=body.platform or None
        )
    # Admin gate via legacy on anchor, then fan-out status.
    legacy.approve_learned_rule(
        body.owner, body.repo, body.id, request, platform=body.platform or None
    )
    scope.fanout_learned_status(copies, "approved")
    return {"ok": True}


@router.post("/api/rules/reject")
def reject_unified_rule(body: UnifiedRuleRef, request: Request) -> dict:
    if body.kind != "learned":
        raise HTTPException(status_code=400, detail="Only learned rules can be rejected")
    from mira.dashboard import rule_scope as scope

    with _open_store(body.owner, body.repo, platform=body.platform or None) as store:
        existing = store.get_learned_rule(body.id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    copies = scope.find_learned_copies(
        owner=body.owner,
        repo=body.repo,
        platform=body.platform or "github",
        rule_id=body.id,
        group_id=existing.group_id or "",
    )
    legacy.reject_learned_rule(
        body.owner, body.repo, body.id, request, platform=body.platform or None
    )
    if len(copies) > 1:
        scope.fanout_learned_status(copies, "rejected")
    return {"ok": True}


@router.post("/api/rules/clear-pending")
def clear_pending_learned_rules(
    request: Request,
    repo: RepoQ = None,
) -> dict:
    """Admin: delete auto-synth Pending learnings (keep @remember / hand-authored).

    Does not reject — clean slate for Rebuild/smoke. Optional ``repo=owner/name``.
    """
    from mira.dashboard import rule_scope as scope
    from mira.dashboard.routers.rules import _require_admin

    _require_admin(request)
    owner = ""
    name = ""
    raw = (repo or "").strip()
    if raw and raw not in ("__all__", "all"):
        if "/" not in raw:
            raise HTTPException(status_code=400, detail="repo must be owner/name")
        owner, name = raw.split("/", 1)
        owner, name = owner.strip(), name.strip()
        if not owner or not name:
            raise HTTPException(status_code=400, detail="repo must be owner/name")
    cleared = scope.clear_pending_auto_synth(owner=owner, repo=name)
    return {"cleared": cleared}


@router.patch("/api/rules/enabled", response_model=UnifiedRule)
def set_unified_rule_enabled(body: UnifiedRuleEnabled, request: Request) -> UnifiedRule:
    if body.kind == "written_global":
        r = legacy.toggle_global_rule(body.id)
        if r.enabled != body.enabled:
            r = legacy.toggle_global_rule(body.id)
        return UnifiedRule(
            id=r.id,
            kind="written_global",
            title=r.title,
            text=r.content,
            enabled=r.enabled,
            status="approved",
            priority="written",
            updated_at=r.updated_at,
        )
    if body.kind == "written_repo":
        return get_unified_rule(
            "written_repo",
            body.id,
            request,
            owner=body.owner,
            repo=body.repo,
            platform=body.platform or "github",
        )
    if body.kind == "learned":
        from mira.dashboard import rule_scope as scope

        with _open_store(body.owner, body.repo, platform=body.platform or None) as store:
            existing = store.get_learned_rule(body.id)
        if not existing:
            raise HTTPException(status_code=404, detail="Rule not found")
        copies = scope.find_learned_copies(
            owner=body.owner,
            repo=body.repo,
            platform=body.platform or "github",
            rule_id=body.id,
            group_id=existing.group_id or "",
        )
        legacy.set_learned_rule_active(
            body.owner,
            body.repo,
            body.id,
            LearnedRuleActiveInput(active=body.enabled),
            request,
            platform=body.platform or None,
        )
        if len(copies) > 1:
            scope.fanout_learned_active(copies, body.enabled)
        return get_unified_rule(
            "learned",
            body.id,
            request,
            owner=body.owner,
            repo=body.repo,
            platform=body.platform or "github",
        )
    raise HTTPException(status_code=400, detail=f"Unknown kind {body.kind}")
