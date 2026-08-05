"""Tests for learnings history backfill and approved rule_text freeze."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.dashboard.db import AppDatabase
from mira.index.store import IndexStore
from mira.platforms.github import learning_backfill as lb


@pytest.fixture
def db(tmp_path: Path) -> AppDatabase:
    return AppDatabase(url=str(tmp_path / "app.db"), admin_password="admin")


@pytest.fixture
def store(tmp_path: Path):
    s = IndexStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _merged_pr(number: int, author: str = "alice") -> MagicMock:
    pr = MagicMock()
    pr.number = number
    pr.merged_at = datetime(2024, 6, 1, tzinfo=UTC)
    pr.user = MagicMock(login=author)
    pr.merged_by = MagicMock(login="bob")
    pr.title = f"PR {number}"
    pr.body = ""
    pr.html_url = f"https://github.com/o/r/pull/{number}"
    pr.base = MagicMock(ref="main")
    pr.head = MagicMock(ref="feat", sha="abc")
    return pr


def test_list_merged_prs_respects_max_prs() -> None:
    prs = [_merged_pr(i) for i in range(1, 6)]
    # Mix in a closed-unmerged PR that should be skipped.
    closed = MagicMock()
    closed.merged_at = None
    closed.number = 99

    gh = MagicMock()
    repo = MagicMock()
    pulls = MagicMock()
    pulls.__iter__ = lambda self: iter([closed, *reversed(prs)])
    repo.get_pulls.return_value = pulls
    gh.get_repo.return_value = repo

    orig = lb.Github
    lb.Github = lambda _token: gh  # type: ignore[assignment]
    try:
        out = lb._list_merged_prs_sync("tok", "o", "r", since=None, max_prs=2)
    finally:
        lb.Github = orig

    assert len(out) == 2
    assert {p["number"] for p in out} == {5, 4}


def test_list_merged_prs_since_skips_not_breaks() -> None:
    """updated-desc list can interleave old/new merges — since must continue."""
    old = _merged_pr(1)
    old.merged_at = datetime(2020, 1, 1, tzinfo=UTC)
    recent = _merged_pr(2)
    recent.merged_at = datetime(2024, 6, 1, tzinfo=UTC)
    # Newer-updated but older-merged first, then a recent merge.
    old.updated_at = datetime(2024, 7, 1, tzinfo=UTC)

    gh = MagicMock()
    repo = MagicMock()
    pulls = MagicMock()
    pulls.__iter__ = lambda self: iter([old, recent])
    repo.get_pulls.return_value = pulls
    gh.get_repo.return_value = repo

    since = datetime(2023, 1, 1, tzinfo=UTC).timestamp()
    orig = lb.Github
    lb.Github = lambda _token: gh  # type: ignore[assignment]
    try:
        out = lb._list_merged_prs_sync("tok", "o", "r", since=since, max_prs=10)
    finally:
        lb.Github = orig

    assert [p["number"] for p in out] == [2]


def test_upsert_freezes_approved_rule_text(store: IndexStore) -> None:
    rule = store.upsert_learned_rule(
        rule_text="original text",
        source_signal="reject_pattern",
        category="style",
        path_pattern="",
        sample_count=3,
    )
    store.set_learned_rule_status(rule.id, "approved")
    updated = store.upsert_learned_rule(
        rule_text="should not overwrite",
        source_signal="reject_pattern",
        category="style",
        path_pattern="",
        sample_count=10,
    )
    assert updated.rule_text == "original text"
    assert updated.sample_count == 10
    assert updated.status == "approved"


def test_upsert_updates_pending_rule_text(store: IndexStore) -> None:
    store.upsert_learned_rule(
        rule_text="pending v1",
        source_signal="reject_pattern",
        category="bug",
        path_pattern="",
        sample_count=3,
    )
    updated = store.upsert_learned_rule(
        rule_text="pending v2",
        source_signal="reject_pattern",
        category="bug",
        path_pattern="",
        sample_count=5,
    )
    assert updated.rule_text == "pending v2"
    assert updated.status == "pending"


def test_has_pr_merge_learning_not_truncated_by_list_limit(store: IndexStore) -> None:
    """Skip must find this PR even when thousands of newer events exist."""
    store.record_feedback(
        pr_number=7,
        pr_url="https://github.com/o/r/pull/7",
        comment_path="",
        comment_line=0,
        comment_category="",
        comment_severity="",
        comment_title="",
        signal="merge_scanned",
        actor="bob",
    )
    # Flood with newer events so PR 7 falls out of the recent-2000 window.
    for i in range(2100):
        store.record_feedback(
            pr_number=1000 + i,
            pr_url=f"https://github.com/o/r/pull/{1000 + i}",
            comment_path="x.py",
            comment_line=1,
            comment_category="style",
            comment_severity="",
            comment_title="n",
            signal="accepted",
            actor="a",
        )
    recent = store.list_feedback(limit=2000)
    assert not any(e.pr_number == 7 for e in recent)
    assert store.has_pr_merge_learning(7) is True
    assert store.has_pr_merge_learning(999999) is False


@pytest.mark.asyncio
async def test_empty_pr_records_merge_scanned_and_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    from mira.models import PRInfo
    from mira.platforms.handlers import run_pr_merged_learning

    provider = MagicMock()
    provider.get_all_bot_threads = AsyncMock(return_value=[])
    provider.get_human_review_comments = AsyncMock(return_value=[])
    pr = PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="f",
        url="https://github.com/o/r/pull/3",
        number=3,
        owner="o",
        repo="r",
        author="alice",
    )
    r1 = await run_pr_merged_learning(provider, pr, "mira", "bob", synthesize=False)
    r2 = await run_pr_merged_learning(provider, pr, "mira", "bob", synthesize=False)
    assert r1["skipped"] == 0 and r2["skipped"] == 1
    store = IndexStore.open("o", "r", platform="github")
    try:
        assert store.has_pr_merge_learning(3)
        assert sum(1 for e in store.list_feedback_for_pr(3) if e.signal == "merge_scanned") == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_backfill_repo_is_idempotent(db: AppDatabase, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    db.register_repo("o", "r", installation_id=1, platform="github")

    merged = [
        {
            "number": 1,
            "title": "One",
            "url": "https://github.com/o/r/pull/1",
            "author": "alice",
            "merged_by": "bob",
            "base_branch": "main",
            "head_branch": "f",
            "head_sha": "sha",
            "body": "",
        }
    ]

    call_count = {"n": 0}

    async def fake_learn(
        provider, pr_info, bot_name, merged_by, platform="github", *, synthesize=True
    ):
        call_count["n"] += 1
        from mira.index.store import IndexStore

        store = IndexStore.open("o", "r", platform="github")
        try:
            existing = store.list_feedback(limit=2000)
            if any(e.pr_number == pr_info.number and e.signal == "human_review" for e in existing):
                return {
                    "accepted": 0,
                    "human_recorded": 0,
                    "deterministic_rules": 0,
                    "llm_rules": 0,
                    "skipped": 1,
                }
            store.record_bulk_feedback(
                [
                    {
                        "pr_number": pr_info.number,
                        "pr_url": pr_info.url,
                        "comment_path": "a.py",
                        "comment_line": 1,
                        "comment_category": "human_review",
                        "comment_severity": "",
                        "comment_title": "prefer guards",
                        "signal": "human_review",
                        "actor": "alice",
                        "pr_author": "alice",
                    }
                ]
            )
            return {
                "accepted": 0,
                "human_recorded": 1,
                "deterministic_rules": 0,
                "llm_rules": 0,
                "skipped": 0,
            }
        finally:
            store.close()

    async def fake_synth(owner, repo, platform="github", *, on_progress=None):
        return {"deterministic_rules": 0, "llm_rules": 0}

    auth = MagicMock()
    auth.get_installation_token = AsyncMock(return_value="tok")

    with (
        patch.object(lb, "_list_merged_prs_sync", return_value=merged),
        patch("mira.providers.create_provider", return_value=MagicMock()),
        patch(
            "mira.platforms.handlers.run_pr_merged_learning",
            side_effect=fake_learn,
        ),
        patch(
            "mira.platforms.handlers.synthesize_repo_learnings",
            side_effect=fake_synth,
        ),
    ):
        c1 = await lb.backfill_repo_learnings("o", "r", auth, installation_id=1, max_prs=10)
        c2 = await lb.backfill_repo_learnings("o", "r", auth, installation_id=1, max_prs=10)

    assert c1["human_recorded"] == 1 and c1["skipped"] == 0
    assert c2["skipped"] == 1 and c2["human_recorded"] == 0
    assert call_count["n"] == 2

    status = lb.get_backfill_status(db, "o", "r")
    assert status.get("status") == "complete"

    store = IndexStore.open("o", "r", platform="github")
    try:
        events = [e for e in store.list_feedback() if e.signal == "human_review"]
        assert len(events) == 1
    finally:
        store.close()


def test_maybe_wait_for_rate_limit_uses_resources_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyGithub >=2.7: RateLimitOverview has no .core — read .resources.core."""
    core = MagicMock(remaining=5000, reset=datetime(2099, 1, 1, tzinfo=UTC))
    overview = MagicMock(spec=["resources"])
    overview.resources.core = core
    del overview.core

    gh = MagicMock()
    gh.get_rate_limit.return_value = overview
    slept: list[float] = []
    monkeypatch.setattr(lb.time, "sleep", slept.append)

    lb._maybe_wait_for_rate_limit(gh)
    assert slept == []


def test_maybe_wait_for_rate_limit_sleeps_when_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = MagicMock(remaining=50, reset=datetime(2099, 1, 1, tzinfo=UTC))
    overview = MagicMock()
    overview.resources.core = core

    gh = MagicMock()
    gh.get_rate_limit.return_value = overview
    slept: list[float] = []
    monkeypatch.setattr(lb.time, "sleep", slept.append)
    monkeypatch.setattr(lb.time, "time", lambda: core.reset.timestamp() - 100)

    lb._maybe_wait_for_rate_limit(gh)
    assert len(slept) == 1
    assert slept[0] == 105


def test_mark_repos_queued_not_running(db: AppDatabase) -> None:
    lb.mark_repos_running(db, [("acme", "web"), ("acme", "api")], max_prs=100)
    a = lb.get_backfill_status(db, "acme", "web")
    b = lb.get_backfill_status(db, "acme", "api")
    assert a["status"] == "queued"
    assert a["job"] == "backfill"
    assert a["max_prs"] == 100
    assert a["total"] == 0
    assert b["status"] == "queued"


def test_mark_repos_synth(db: AppDatabase) -> None:
    lb.mark_repos_synth(db, [("acme", "web")])
    st = lb.get_backfill_status(db, "acme", "web")
    assert st["status"] == "queued"
    assert st["job"] == "synth"
    assert st["phase"] == "queued"


def test_repos_with_active_job(db: AppDatabase) -> None:
    lb.mark_repos_synth(db, [("acme", "web")])
    lb._set_status(db, "acme", "api", status="complete", job="synth")
    assert lb.repos_with_active_job(db, [("acme", "web"), ("acme", "api")]) == [("acme", "web")]


@pytest.mark.asyncio
async def test_synthesize_repo_with_progress_writes_complete(
    db: AppDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mira.dashboard.api as api_mod

    monkeypatch.setattr(api_mod, "_app_db", db)

    async def fake_synth(owner, repo, platform="github", *, on_progress=None):
        if on_progress:
            on_progress({"phase": "classify", "classify_done": 1, "classify_total": 1})
            on_progress({"phase": "extract", "extract_done": 2, "extract_total": 2})
            on_progress({"phase": "cluster"})
            on_progress({"phase": "complete", "upserted": 1})
        return {"deterministic_rules": 0, "llm_rules": 1}

    with patch(
        "mira.platforms.handlers.synthesize_repo_learnings",
        new=fake_synth,
    ):
        lb.mark_repos_synth(db, [("acme", "web")])
        counts = await lb.synthesize_repo_with_progress("acme", "web", platform="github")

    assert counts["llm_rules"] == 1
    st = lb.get_backfill_status(db, "acme", "web")
    assert st["status"] == "complete"
    assert st["job"] == "synth"
    assert st["phase"] == "complete"
    assert st["llm_rules"] == 1
