"""@bot remember → pending learned rule."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.platforms.handlers import run_pr_command


@pytest.mark.asyncio
async def test_remember_creates_pending_learning(tmp_path):
    from mira.index.store import IndexStore

    db = tmp_path / "r.db"
    store = IndexStore(str(db))
    store.close()

    provider = AsyncMock()
    provider.get_pr_info = AsyncMock(
        return_value=SimpleNamespace(
            title="t",
            description="",
            base_branch="main",
            head_branch="f",
            url="https://github.com/o/r/pull/9",
            number=9,
            owner="o",
            repo="r",
        )
    )
    provider.post_comment = AsyncMock()

    with (
        patch("mira.platforms.handlers.load_config", return_value=MagicMock()),
        patch("mira.platforms.handlers.create_llm", return_value=AsyncMock()),
        patch(
            "mira.platforms.handlers._open_store",
            return_value=IndexStore(str(db)),
        ),
    ):
        await run_pr_command(
            provider=provider,
            pr_url="https://github.com/o/r/pull/9",
            owner="o",
            repo="r",
            number=9,
            question="remember Prefer Alpine base images",
            actor="alice",
            bot_name="mira",
            pr_title="t",
            platform="github",
        )

    check = IndexStore(str(db))
    try:
        rules = check.list_learned_rules()
        assert len(rules) == 1
        r = rules[0]
        assert r.rule_text == "Prefer Alpine base images"
        assert r.status == "pending"
        assert r.source_signal == "manual"
        assert r.created_by == "alice"
        assert "9" in (r.evidence_prs or "")
    finally:
        check.close()

    provider.post_comment.assert_awaited_once()
    body = provider.post_comment.await_args.args[1]
    assert "pending" in body.lower()


@pytest.mark.asyncio
async def test_remember_near_dupe_bumps_existing(tmp_path):
    from mira.index.store import IndexStore

    db = tmp_path / "r.db"
    store = IndexStore(str(db))
    existing = store.create_learned_rule(
        rule_text="Prefer Alpine base images",
        category="human_review",
        path_pattern="__human_x__",
        source_signal="manual",
        status="pending",
        created_by="bob",
    )
    store.close()

    provider = AsyncMock()
    provider.get_pr_info = AsyncMock(return_value=SimpleNamespace())
    provider.post_comment = AsyncMock()

    with (
        patch("mira.platforms.handlers.load_config", return_value=MagicMock()),
        patch("mira.platforms.handlers.create_llm", return_value=AsyncMock()),
        patch(
            "mira.platforms.handlers._open_store",
            return_value=IndexStore(str(db)),
        ),
    ):
        await run_pr_command(
            provider=provider,
            pr_url="https://github.com/o/r/pull/3",
            owner="o",
            repo="r",
            number=3,
            question="remember Prefer Alpine base images",
            actor="alice",
            bot_name="mira",
            pr_title="t",
        )

    check = IndexStore(str(db))
    try:
        rules = check.list_learned_rules()
        assert len(rules) == 1
        assert rules[0].id == existing.id
        assert rules[0].sample_count >= 1
        assert "3" in (rules[0].evidence_prs or "")
    finally:
        check.close()

    body = provider.post_comment.await_args.args[1]
    assert "reinforced" in body.lower()
    assert "Rules → Pending" in body


@pytest.mark.asyncio
async def test_remember_without_body_posts_usage():
    provider = AsyncMock()
    provider.get_pr_info = AsyncMock(return_value=SimpleNamespace())
    provider.post_comment = AsyncMock()

    with (
        patch("mira.platforms.handlers.load_config", return_value=MagicMock()),
        patch("mira.platforms.handlers.create_llm", return_value=AsyncMock()),
    ):
        await run_pr_command(
            provider=provider,
            pr_url="https://github.com/o/r/pull/1",
            owner="o",
            repo="r",
            number=1,
            question="remember",
            actor="alice",
            bot_name="mira",
            pr_title="t",
        )

    body = provider.post_comment.await_args.args[1]
    assert "usage" in body.lower()
