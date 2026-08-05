"""Rule scope migrate / fan-out: backend copies, one product row via group_id."""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import HTTPException

from mira.dashboard.api import RuleCreate, ScopeRepoRef, UnifiedRule, _open_store
from mira.dashboard.routers import rules as legacy


def _repo_key(platform: str, owner: str, repo: str) -> str:
    return f"{platform or 'github'}|{owner}/{repo}"


def _list_org_learned_raw(status: str | None = None) -> list[dict]:
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import list_learned_rules_org_wide

        return list_learned_rules_org_wide(db_url, limit=5000, status=status)
    from mira.index.store import list_learned_rules_org_wide_sqlite

    return list_learned_rules_org_wide_sqlite(limit=5000, status=status)


def find_learned_copies(
    *,
    owner: str,
    repo: str,
    platform: str,
    rule_id: int,
    group_id: str = "",
) -> list[dict]:
    """All store rows for this product rule (singleton or group)."""
    rows = _list_org_learned_raw(None)
    if group_id:
        return [r for r in rows if (r.get("group_id") or "") == group_id]
    return [
        r
        for r in rows
        if int(r.get("id") or 0) == rule_id
        and r.get("owner") == owner
        and r.get("repo") == repo
        and (r.get("platform") or "github") == (platform or "github")
    ]


def collapse_learned_rows(rows: list[UnifiedRule]) -> list[UnifiedRule]:
    """One UnifiedRule per group_id; singletons unchanged."""
    groups: dict[str, list[UnifiedRule]] = {}
    singles: list[UnifiedRule] = []
    for r in rows:
        gid = (r.group_id or "").strip()
        if not gid:
            singles.append(
                r.model_copy(
                    update={
                        "repos": (
                            r.repos
                            if r.repos
                            else ([f"{r.owner}/{r.repo}"] if r.owner and r.repo else [])
                        )
                    }
                )
            )
            continue
        groups.setdefault(gid, []).append(r)

    out: list[UnifiedRule] = list(singles)
    for gid, members in groups.items():
        members.sort(key=lambda m: (m.owner, m.repo, m.id))
        anchor = max(members, key=lambda m: (m.updated_at, m.id))
        repos = sorted({f"{m.owner}/{m.repo}" for m in members if m.owner and m.repo})
        out.append(
            UnifiedRule(
                id=anchor.id,
                kind="learned",
                owner=anchor.owner,
                repo=anchor.repo,
                platform=anchor.platform,
                title=anchor.title,
                text=anchor.text,
                enabled=all(m.enabled for m in members),
                status=anchor.status,
                category=anchor.category,
                path_pattern=anchor.path_pattern,
                source_signal=anchor.source_signal,
                sample_count=max(m.sample_count for m in members),
                evidence_prs=anchor.evidence_prs,
                created_by=anchor.created_by,
                priority="learned",
                updated_at=max(m.updated_at for m in members),
                group_id=gid,
                repos=repos,
            )
        )
    return out


def attach_learned_repos(row: UnifiedRule) -> UnifiedRule:
    """Fill repos[] for get/detail from group or singleton."""
    if row.kind != "learned":
        return row
    copies = find_learned_copies(
        owner=row.owner,
        repo=row.repo,
        platform=row.platform,
        rule_id=row.id,
        group_id=row.group_id,
    )
    if not copies and row.owner and row.repo:
        return row.model_copy(update={"repos": [f"{row.owner}/{row.repo}"]})
    repos = sorted(
        {f"{c['owner']}/{c['repo']}" for c in copies if c.get("owner") and c.get("repo")}
    )
    gid = row.group_id or (str(copies[0].get("group_id") or "") if copies else "")
    return row.model_copy(update={"repos": repos, "group_id": gid})


def _for_each_copy(copies: list[dict], fn: Any) -> None:
    for c in copies:
        with _open_store(
            str(c["owner"]),
            str(c["repo"]),
            platform=str(c.get("platform") or "github"),
        ) as store:
            fn(store, int(c["id"]))


def fanout_learned_status(copies: list[dict], status: str) -> None:
    _for_each_copy(copies, lambda store, rid: store.set_learned_rule_status(rid, status))


def fanout_learned_active(copies: list[dict], active: bool) -> None:
    _for_each_copy(copies, lambda store, rid: store.set_learned_rule_active(rid, active))


def fanout_learned_delete(copies: list[dict]) -> None:
    _for_each_copy(copies, lambda store, rid: store.delete_learned_rule(rid))


def promote_learned_to_global(
    *,
    owner: str,
    repo: str,
    platform: str,
    rule_id: int,
    text: str,
    title: str,
) -> UnifiedRule:
    with _open_store(owner, repo, platform=platform or None) as store:
        existing = store.get_learned_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    copies = find_learned_copies(
        owner=owner,
        repo=repo,
        platform=platform or "github",
        rule_id=rule_id,
        group_id=existing.group_id or "",
    )
    created = legacy.create_global_rule(
        RuleCreate(title=(title or "Rule").strip() or "Rule", content=text.strip())
    )
    fanout_learned_delete(
        copies
        or [
            {
                "owner": owner,
                "repo": repo,
                "platform": platform or "github",
                "id": rule_id,
            }
        ]
    )
    return UnifiedRule(
        id=created.id,
        kind="written_global",
        title=created.title,
        text=created.content,
        enabled=created.enabled,
        status="approved",
        priority="written",
        updated_at=created.updated_at,
        repos=[],
    )


def sync_learned_repos(
    *,
    owner: str,
    repo: str,
    platform: str,
    rule_id: int,
    text: str,
    category: str,
    path_pattern: str,
    scope_repos: list[ScopeRepoRef],
) -> UnifiedRule:
    """Ensure exactly ``scope_repos`` hold copies; group when 2+."""
    if not scope_repos:
        raise HTTPException(status_code=400, detail="Pick at least one repo")

    wanted: dict[str, ScopeRepoRef] = {}
    for ref in scope_repos:
        if not ref.owner or not ref.repo:
            continue
        wanted[_repo_key(ref.platform, ref.owner, ref.repo)] = ScopeRepoRef(
            owner=ref.owner,
            repo=ref.repo,
            platform=ref.platform or "github",
        )
    if not wanted:
        raise HTTPException(status_code=400, detail="Pick at least one repo")

    with _open_store(owner, repo, platform=platform or None) as store:
        source = store.get_learned_rule(rule_id)
    if not source:
        raise HTTPException(status_code=404, detail="Rule not found")

    copies = find_learned_copies(
        owner=owner,
        repo=repo,
        platform=platform or "github",
        rule_id=rule_id,
        group_id=source.group_id or "",
    )
    if not copies:
        copies = [
            {
                "id": source.id,
                "owner": owner,
                "repo": repo,
                "platform": platform or "github",
                "group_id": source.group_id or "",
                "status": source.status,
                "active": source.active,
                "created_by": source.created_by,
                "source_signal": source.source_signal,
                "evidence_prs": source.evidence_prs,
                "sample_count": source.sample_count,
                "rule_text": source.rule_text,
                "category": source.category,
                "path_pattern": source.path_pattern,
            }
        ]

    gid = (source.group_id or "").strip()
    gid = gid or uuid.uuid4().hex[:16] if len(wanted) > 1 else ""

    existing_by_key = {
        _repo_key(str(c.get("platform") or "github"), str(c["owner"]), str(c["repo"])): c
        for c in copies
    }

    for key, c in list(existing_by_key.items()):
        if key not in wanted:
            with _open_store(
                str(c["owner"]),
                str(c["repo"]),
                platform=str(c.get("platform") or "github"),
            ) as store:
                store.delete_learned_rule(int(c["id"]))
            del existing_by_key[key]

    template = copies[0]
    status = str(template.get("status") or source.status)
    active = bool(template.get("active", source.active))
    created_by = str(template.get("created_by") or source.created_by or "")
    source_signal = str(template.get("source_signal") or source.source_signal or "manual")
    evidence_prs = str(template.get("evidence_prs") or source.evidence_prs or "")
    sample_count = int(template.get("sample_count") or source.sample_count or 0)

    anchor: dict | None = None
    for key, ref in wanted.items():
        if key in existing_by_key:
            c = existing_by_key[key]
            with _open_store(ref.owner, ref.repo, platform=ref.platform) as store:
                store.update_learned_rule(
                    int(c["id"]),
                    text,
                    category,
                    path_pattern,
                    group_id=gid,
                )
            c = {
                **c,
                "rule_text": text,
                "category": category,
                "path_pattern": path_pattern,
                "group_id": gid,
            }
            existing_by_key[key] = c
            if anchor is None:
                anchor = c
            continue
        with _open_store(ref.owner, ref.repo, platform=ref.platform) as store:
            created = store.create_learned_rule(
                rule_text=text,
                category=category,
                path_pattern=path_pattern,
                source_signal=source_signal,
                status=status,
                active=active,
                created_by=created_by,
                group_id=gid,
                evidence_prs=evidence_prs if len(wanted) == 1 else "",
                sample_count=sample_count if len(wanted) == 1 else 0,
            )
        c = {
            "id": created.id,
            "owner": ref.owner,
            "repo": ref.repo,
            "platform": ref.platform,
            "group_id": gid,
            "status": status,
            "active": active,
            "created_by": created_by,
            "source_signal": source_signal,
            "evidence_prs": created.evidence_prs,
            "sample_count": created.sample_count,
            "rule_text": text,
            "category": category,
            "path_pattern": path_pattern,
            "updated_at": created.updated_at,
        }
        existing_by_key[key] = c
        if anchor is None:
            anchor = c

    assert anchor is not None
    from mira.analysis.learned_rules import strip_synth_rationale

    repos = sorted({f"{c['owner']}/{c['repo']}" for c in existing_by_key.values()})
    return UnifiedRule(
        id=int(anchor["id"]),
        kind="learned",
        owner=str(anchor["owner"]),
        repo=str(anchor["repo"]),
        platform=str(anchor.get("platform") or "github"),
        text=strip_synth_rationale(text),
        enabled=bool(anchor.get("active", True)),
        status=str(anchor.get("status") or status),
        category=category,
        path_pattern=path_pattern,
        source_signal=str(anchor.get("source_signal") or source_signal),
        sample_count=int(anchor.get("sample_count") or 0),
        evidence_prs=str(anchor.get("evidence_prs") or ""),
        created_by=str(anchor.get("created_by") or ""),
        priority="learned",
        updated_at=float(anchor.get("updated_at") or 0),
        group_id=gid,
        repos=repos,
    )


def migrate_written_scope(
    *,
    kind: str,
    rule_id: int,
    owner: str,
    repo: str,
    platform: str,
    title: str,
    text: str,
    scope: str,
    scope_repos: list[ScopeRepoRef] | None,
) -> UnifiedRule:
    """Global ↔ single per-repo. Multi-repo written → use Global."""
    title = (title or "Rule").strip() or "Rule"
    text = text.strip()
    if scope == "global":
        if kind == "written_global":
            r = legacy.update_global_rule(rule_id, RuleCreate(title=title, content=text))
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
        created = legacy.create_global_rule(RuleCreate(title=title, content=text))
        legacy.delete_repo_rule(owner, repo, rule_id)
        return UnifiedRule(
            id=created.id,
            kind="written_global",
            title=created.title,
            text=created.content,
            enabled=created.enabled,
            status="approved",
            priority="written",
            updated_at=created.updated_at,
        )

    refs = scope_repos or []
    if len(refs) != 1:
        raise HTTPException(
            status_code=400,
            detail="Written rules are Global or one repo — use Global for all repos",
        )
    ref = refs[0]
    if kind == "written_repo" and ref.owner == owner and ref.repo == repo:
        r = legacy.update_repo_rule(owner, repo, rule_id, RuleCreate(title=title, content=text))
        return UnifiedRule(
            id=r.id,
            kind="written_repo",
            owner=owner,
            repo=repo,
            platform=platform or "github",
            title=r.title,
            text=r.content,
            enabled=True,
            status="approved",
            priority="written",
            updated_at=getattr(r, "updated_at", 0) or 0,
            repos=[f"{owner}/{repo}"],
        )
    created = legacy.create_repo_rule(ref.owner, ref.repo, RuleCreate(title=title, content=text))
    if kind == "written_global":
        legacy.delete_global_rule(rule_id)
    else:
        legacy.delete_repo_rule(owner, repo, rule_id)
    return UnifiedRule(
        id=created.id,
        kind="written_repo",
        owner=ref.owner,
        repo=ref.repo,
        platform=ref.platform or "github",
        title=created.title,
        text=created.content,
        enabled=True,
        status="approved",
        priority="written",
        updated_at=getattr(created, "updated_at", 0) or 0,
        repos=[f"{ref.owner}/{ref.repo}"],
    )
