"""Unified Rules façade — Pending vs Active."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

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
            title="Prefer Alpine",
            text="Use Alpine.js for small client interactions.",
            owner="acme",
            repo="web",
        ),
        _Req(True, "boss"),
    )
    # pending should not appear in catalog
    ur.create_unified_rule(
        api.UnifiedRuleCreate(
            kind="learned",
            title="Pending only",
            text="This pending learning should stay in Pending.",
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
    assert not any(r.title == "Pending only" for r in catalog)

    inbox = ur.list_unified_rules(mode="pending")
    assert any(r.title == "Pending only" for r in inbox)
    assert all(r.kind == "learned" and r.status == "pending" for r in inbox)

    # Kind filter must not empty the inbox (pending is always learned).
    inbox_filtered = ur.list_unified_rules(mode="pending", kind="written_repo")
    assert any(r.title == "Pending only" for r in inbox_filtered)


def test_mode_aliases_inbox_catalog(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    ur.create_unified_rule(
        api.UnifiedRuleCreate(
            kind="learned",
            title="Alias check",
            text="Mode aliases should map inbox to pending.",
            owner="acme",
            repo="web",
        ),
        _Req(False, "junior"),
    )
    assert len(ur.list_unified_rules(mode="inbox")) == len(ur.list_unified_rules(mode="pending"))
    assert len(ur.list_unified_rules(mode="catalog")) == len(ur.list_unified_rules(mode="active"))


def test_approve_moves_pending_to_active(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = ur.create_unified_rule(
        api.UnifiedRuleCreate(
            kind="learned",
            title="Ship it",
            text="Approve moves pending learnings into Active.",
            owner="acme",
            repo="web",
        ),
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
        api.UnifiedRuleCreate(
            kind="learned",
            title="Toggle me",
            text="Enabled flag can be toggled on approved learnings.",
            owner="acme",
            repo="web",
        ),
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
            kind="learned",
            title="Lock immutable props",
            text="Use #[Locked] on immutable props",
            owner="acme",
            repo="web",
        ),
        _Req(False, "junior"),
    )
    updated = ur.update_unified_rule(
        api.UnifiedRuleUpdate(
            kind="learned",
            id=created.id,
            owner="acme",
            repo="web",
            title="Lock immutable props",
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
            kind="learned",
            title="Enums",
            text="Prefer enums over magic strings",
            owner="acme",
            repo="web",
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


def test_clear_pending_deletes_auto_keeps_remember(patched_db: AppDatabase):
    from mira.analysis.learned_rules import pack_learned_rule

    patched_db.register_repo("acme", "web")
    patched_db.register_repo("acme", "api")
    store_web = IndexStore.open("acme", "web")
    auto = store_web.upsert_learned_rule(
        rule_text=pack_learned_rule(
            "Auto mush",
            "When reviewing, this auto-synth pending row should be cleared by the bulk action.",
        ),
        source_signal="human_pattern",
        category="human_review",
        path_pattern="__human_auto__",
        sample_count=1,
        status="pending",
    )
    remember = store_web.create_learned_rule(
        rule_text=pack_learned_rule(
            "Remember tip",
            "Hand-authored pending should survive clear-pending for smoke testing.",
        ),
        source_signal="human_pattern",
        category="other",
        path_pattern="",
        sample_count=1,
        status="pending",
        created_by="alice",
    )
    store_web.close()
    store_api = IndexStore.open("acme", "api")
    other = store_api.upsert_learned_rule(
        rule_text=pack_learned_rule(
            "Other repo auto",
            "Auto pending on another repo should clear when scope is org-wide.",
        ),
        source_signal="human_pattern",
        category="human_review",
        path_pattern="__human_api__",
        sample_count=1,
        status="pending",
    )
    store_api.close()

    with pytest.raises(HTTPException) as exc:
        ur.clear_pending_learned_rules(_Req(False), repo=None)
    assert exc.value.status_code == 403

    cleared_web = ur.clear_pending_learned_rules(_Req(True), repo="acme/web")
    assert cleared_web["cleared"] == 1
    store_web = IndexStore.open("acme", "web")
    assert store_web.get_learned_rule(auto.id) is None
    assert store_web.get_learned_rule(remember.id) is not None
    store_web.close()
    store_api = IndexStore.open("acme", "api")
    assert store_api.get_learned_rule(other.id) is not None
    store_api.close()

    cleared_all = ur.clear_pending_learned_rules(_Req(True), repo=None)
    assert cleared_all["cleared"] == 1
    store_api = IndexStore.open("acme", "api")
    assert store_api.get_learned_rule(other.id) is None
    store_api.close()
