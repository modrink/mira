"""Unified Rules façade — Pending vs Active."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase, User
from mira.dashboard.routers import unified_rules as ur
from mira.index.store import IndexStore


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    return db


class _Req:
    def __init__(self, is_admin: bool, username: str = "u"):
        self.state = type("S", (), {"user": User(id=1, username=username, is_admin=is_admin)})()


def test_catalog_includes_written_and_approved_learned(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    ur.create_unified_rule(
        api.UnifiedRuleCreate(kind="written_global", title="G", text="global tip"),
        _Req(True),
    )
    ur.create_unified_rule(
        api.UnifiedRuleCreate(
            kind="written_repo",
            title="R",
            text="repo tip",
            owner="acme",
            repo="web",
        ),
        _Req(True),
    )
    ur.create_unified_rule(
        api.UnifiedRuleCreate(
            kind="learned",
            text="Prefer Alpine",
            owner="acme",
            repo="web",
        ),
        _Req(True, "boss"),
    )
    # pending should not appear in catalog
    ur.create_unified_rule(
        api.UnifiedRuleCreate(
            kind="learned",
            text="Pending only",
            owner="acme",
            repo="web",
        ),
        _Req(False, "junior"),
    )

    catalog = ur.list_unified_rules(mode="active")
    kinds = {r.kind for r in catalog}
    assert "written_global" in kinds
    assert "written_repo" in kinds
    assert "learned" in kinds
    assert all(r.status == "approved" for r in catalog if r.kind == "learned")
    assert not any(r.text == "Pending only" for r in catalog)

    inbox = ur.list_unified_rules(mode="pending")
    assert any(r.text == "Pending only" for r in inbox)
    assert all(r.kind == "learned" and r.status == "pending" for r in inbox)

    # Kind filter must not empty the inbox (pending is always learned).
    inbox_filtered = ur.list_unified_rules(mode="pending", kind="written_repo")
    assert any(r.text == "Pending only" for r in inbox_filtered)


def test_mode_aliases_inbox_catalog(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    ur.create_unified_rule(
        api.UnifiedRuleCreate(kind="learned", text="Alias check", owner="acme", repo="web"),
        _Req(False, "junior"),
    )
    assert len(ur.list_unified_rules(mode="inbox")) == len(ur.list_unified_rules(mode="pending"))
    assert len(ur.list_unified_rules(mode="catalog")) == len(ur.list_unified_rules(mode="active"))


def test_approve_moves_pending_to_active(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = ur.create_unified_rule(
        api.UnifiedRuleCreate(kind="learned", text="Ship it", owner="acme", repo="web"),
        _Req(False, "junior"),
    )
    assert created.status == "pending"
    ur.approve_unified_rule(
        api.UnifiedRuleRef(kind="learned", id=created.id, owner="acme", repo="web"),
        _Req(True),
    )
    assert not any(r.id == created.id for r in ur.list_unified_rules(mode="pending"))
    assert any(
        r.id == created.id and r.status == "approved"
        for r in ur.list_unified_rules(mode="active", kind="learned")
    )


def test_set_enabled_learned(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = ur.create_unified_rule(
        api.UnifiedRuleCreate(kind="learned", text="Toggle me", owner="acme", repo="web"),
        _Req(True),
    )
    disabled = ur.set_unified_rule_enabled(
        api.UnifiedRuleEnabled(
            kind="learned",
            id=created.id,
            enabled=False,
            owner="acme",
            repo="web",
        ),
        _Req(True),
    )
    assert disabled.enabled is False
    store = IndexStore.open("acme", "web")
    assert store.get_learned_rule(created.id).active is False
    store.close()


def test_learned_scope_fanout_groups_list(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    patched_db.register_repo("acme", "api")
    created = ur.create_unified_rule(
        api.UnifiedRuleCreate(
            kind="learned", text="Use #[Locked] on immutable props", owner="acme", repo="web"
        ),
        _Req(False, "junior"),
    )
    updated = ur.update_unified_rule(
        api.UnifiedRuleUpdate(
            kind="learned",
            id=created.id,
            owner="acme",
            repo="web",
            text="Use #[Locked] on immutable props",
            scope="repos",
            scope_repos=[
                api.ScopeRepoRef(owner="acme", repo="web"),
                api.ScopeRepoRef(owner="acme", repo="api"),
            ],
        ),
        _Req(True),
    )
    assert updated.group_id
    assert set(updated.repos) == {"acme/web", "acme/api"}
    pending = ur.list_unified_rules(mode="pending")
    matches = [r for r in pending if "#[Locked]" in r.text]
    assert len(matches) == 1
    assert set(matches[0].repos) == {"acme/web", "acme/api"}

    web = IndexStore.open("acme", "web")
    api_store = IndexStore.open("acme", "api")
    assert any("#[Locked]" in r.rule_text for r in web.list_learned_rules())
    assert any("#[Locked]" in r.rule_text for r in api_store.list_learned_rules())
    web.close()
    api_store.close()


def test_learned_promote_to_global(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = ur.create_unified_rule(
        api.UnifiedRuleCreate(
            kind="learned", text="Prefer enums over magic strings", owner="acme", repo="web"
        ),
        _Req(False, "junior"),
    )
    promoted = ur.update_unified_rule(
        api.UnifiedRuleUpdate(
            kind="learned",
            id=created.id,
            owner="acme",
            repo="web",
            title="Enums",
            text="Prefer enums over magic strings",
            scope="global",
        ),
        _Req(True),
    )
    assert promoted.kind == "written_global"
    assert not any("magic strings" in r.text for r in ur.list_unified_rules(mode="pending"))
    assert any(
        r.kind == "written_global" and "magic strings" in r.text
        for r in ur.list_unified_rules(mode="active")
    )


def test_written_global_to_repo(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = ur.create_unified_rule(
        api.UnifiedRuleCreate(kind="written_global", title="Style", text="No TODOs in prod"),
        _Req(True),
    )
    moved = ur.update_unified_rule(
        api.UnifiedRuleUpdate(
            kind="written_global",
            id=created.id,
            title="Style",
            text="No TODOs in prod",
            scope="repos",
            scope_repos=[api.ScopeRepoRef(owner="acme", repo="web")],
        ),
        _Req(True),
    )
    assert moved.kind == "written_repo"
    assert moved.owner == "acme"
    assert moved.repo == "web"
