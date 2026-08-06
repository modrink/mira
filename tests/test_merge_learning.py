"""Tests for merge-time learning: bot-metadata parsing, accept/reject synthesis,
LLM-powered human-review synthesis, and webhook routing of merged PRs."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.analysis.feedback import (
    _select_human_comments_for_synth,
    llm_synth_cooldown_elapsed,
    mark_llm_synth,
    pack_human_comment_for_learning,
    synthesize_from_human_reviews,
    synthesize_rules,
)
from mira.analysis.learned_rules import pack_learned_rule, unpack_learned_rule
from mira.index.store import FeedbackEventRow, IndexStore
from mira.models import BotThreadRecord, HumanReviewComment
from mira.providers.github import parse_bot_comment_metadata


def _staged_llm_complete(cluster_payload: dict | str):
    """Mock llm.complete for extract → cluster."""
    cluster_json = (
        cluster_payload if isinstance(cluster_payload, str) else json.dumps(cluster_payload)
    )

    async def _complete(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        if "extract one titled coding rule" in prompt:
            idxs = [int(m) for m in re.findall(r"^### (\d+) —", prompt, re.M)]
            return json.dumps(
                {
                    "extractions": [
                        {
                            "index": idx,
                            "title": "Apply the team pattern",
                            "body": (
                                "Follow the rejected approach replacement called out in review."
                            ),
                            "path_hint": "",
                            "prs": [],
                        }
                        for idx in idxs
                    ]
                }
            )
        return cluster_json

    return _complete


def _create(
    title: str,
    *,
    body: str | None = None,
    evidence_count: int = 2,
    prs: list[int] | None = None,
    path_hint: str = "",
) -> dict:
    """Titled create action for synth mocks."""
    out: dict = {
        "action": "create",
        "title": title,
        "body": body or f"{title}.",
        "evidence_count": evidence_count,
        "path_hint": path_hint,
    }
    if prs is not None:
        out["prs"] = prs
    return out


def _merge(
    target_id: int,
    *,
    evidence_count: int = 2,
    title: str = "",
    body: str = "",
    prs: list[int] | None = None,
) -> dict:
    """Merge action; empty title/body keeps existing text."""
    out: dict = {
        "action": "merge",
        "target_id": target_id,
        "evidence_count": evidence_count,
    }
    if title:
        out["title"] = title
    if body:
        out["body"] = body
    if prs is not None:
        out["prs"] = prs
    return out



@pytest.fixture
def store(tmp_path):
    s = IndexStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _human_ev(
    *,
    id: int,
    pr_number: int,
    created_at: float,
    title: str = "review note",
) -> FeedbackEventRow:
    return FeedbackEventRow(
        id=id,
        pr_number=pr_number,
        pr_url=f"https://x/{pr_number}",
        comment_path="a.py",
        comment_line=1,
        comment_category="human_review",
        comment_severity="",
        comment_title=title,
        signal="human_review",
        actor="alice",
        created_at=created_at,
    )


# Simulated bulk-backfill ingest skew: newest PRs ingested first → older
# created_at; last-written small/old PRs get newest timestamps.
_BACKFILL_SKEW: list[tuple[int, int]] = [
    (101, 22),
    (102, 15),
    (103, 5),
    (104, 4),
    (105, 73),
    (106, 9),
    (107, 12),
    (108, 2),
    (109, 1),
    (110, 2),
    (111, 3),
]


def _skewed_backfill_events() -> list[FeedbackEventRow]:
    """Simulate bulk backfill: one created_at per PR, ingest order = list order."""
    events: list[FeedbackEventRow] = []
    next_id = 1
    base_ts = 1_000_000.0
    for batch_i, (pr, n) in enumerate(_BACKFILL_SKEW):
        ts = base_ts + batch_i  # later batches = higher created_at (written last)
        for j in range(n):
            events.append(
                _human_ev(
                    id=next_id,
                    pr_number=pr,
                    created_at=ts,
                    title=f"pr{pr}-c{j}",
                )
            )
            next_id += 1
    return events


class TestSelectHumanCommentsForSynth:
    def test_skewed_ingest_order_old_slice_excludes_fat_early_prs(self):
        events = _skewed_backfill_events()
        # Old behaviour: list_feedback ORDER BY created_at DESC, take [:50].
        newest_first = sorted(
            [e for e in events if e.signal == "human_review"],
            key=lambda e: (e.created_at, e.id),
            reverse=True,
        )
        old_window = newest_first[:50]
        old_prs = {e.pr_number for e in old_window}
        assert 101 not in old_prs
        assert 102 not in old_prs

    def test_stratify_includes_fat_early_prs(self):
        events = _skewed_backfill_events()
        selected = _select_human_comments_for_synth(events, limit=100)
        prs = {e.pr_number for e in selected}
        assert 101 in prs
        assert 102 in prs
        assert 105 in prs
        assert len(selected) == 100
        # Round-robin: no single PR should own the whole window.
        from collections import Counter

        counts = Counter(e.pr_number for e in selected)
        assert max(counts.values()) < 100
        # Every PR with comments gets at least one slot when limit >= n_prs.
        assert prs == {pr for pr, _ in _BACKFILL_SKEW}

    def test_ignores_non_human_signals(self):
        events = [
            _human_ev(id=1, pr_number=1, created_at=1.0),
            FeedbackEventRow(
                id=2,
                pr_number=1,
                pr_url="https://x/1",
                comment_path="a.py",
                comment_line=2,
                comment_category="bug",
                comment_severity="warning",
                comment_title="bot",
                signal="accepted",
                actor="bot",
                created_at=2.0,
            ),
            _human_ev(id=3, pr_number=2, created_at=3.0),
        ]
        selected = _select_human_comments_for_synth(events, limit=10)
        assert len(selected) == 2
        assert all(e.signal == "human_review" for e in selected)

    def test_prefers_hunk_bearing_comments_within_pr(self):

        plain = _human_ev(id=1, pr_number=1, created_at=1.0, title="no hunk first")
        with_hunk = _human_ev(
            id=2,
            pr_number=1,
            created_at=1.0,
            title=pack_human_comment_for_learning(
                "prefer Http client",
                "@@ -1 +1 @@\n-curl\n+Http::get",
            ),
        )
        other_pr = _human_ev(id=3, pr_number=2, created_at=2.0, title="other")
        selected = _select_human_comments_for_synth(
            [plain, with_hunk, other_pr],
            limit=2,
        )
        # Round-robin across PRs still; within PR 1 hunk sorts first so id=2 before id=1
        # when both from same PR would be taken — take limit 3 to see order within PR1.
        selected3 = _select_human_comments_for_synth(
            [plain, with_hunk, other_pr],
            limit=3,
        )
        pr1 = [e for e in selected3 if e.pr_number == 1]
        assert pr1[0].id == 2
        assert pr1[1].id == 1
        assert len(selected) == 2


# ── parse_bot_comment_metadata ──


class TestParseBotCommentMetadata:
    def test_parses_category_severity_title(self):
        body = (
            "🐛 **Bug**\n"
            "🛑 Blocker — must fix before merge\n"
            "\n"
            "**Null pointer on empty input**\n"
            "\n"
            "The function will crash if `items` is empty.\n"
        )
        meta = parse_bot_comment_metadata(body)
        assert meta["category"] == "bug"
        assert meta["severity"] == "blocker"
        assert meta["title"] == "Null pointer on empty input"

    def test_warning_severity(self):
        body = "🔒 **Security issue**\n⚠️ Warning\n\n**Raw SQL query**\n\nbody"
        meta = parse_bot_comment_metadata(body)
        assert meta["category"] == "security"
        assert meta["severity"] == "warning"
        assert meta["title"] == "Raw SQL query"

    def test_no_category_emoji(self):
        # Current format drops the leading category emoji.
        body = (
            "**Security issue**  \n🛑 Blocker — must fix before merge\n\n**Raw SQL query**\n\nbody"
        )
        meta = parse_bot_comment_metadata(body)
        assert meta["category"] == "security"
        assert meta["severity"] == "blocker"
        assert meta["title"] == "Raw SQL query"

    def test_missing_severity_still_extracts_category_and_title(self):
        body = "⚡ **Performance**\n\n**Slow loop**\n\nbody"
        meta = parse_bot_comment_metadata(body)
        assert meta["category"] == "performance"
        assert meta["severity"] == ""
        assert meta["title"] == "Slow loop"

    def test_malformed_body_returns_blanks(self):
        meta = parse_bot_comment_metadata("not a structured comment at all")
        assert meta["category"] == ""
        assert meta["severity"] == ""
        assert meta["title"] == ""

    def test_empty_body_returns_blanks(self):
        meta = parse_bot_comment_metadata("")
        assert meta == {"category": "", "severity": "", "title": ""}


# ── record_bulk_feedback ──


class TestBulkFeedback:
    def test_inserts_multiple_events(self, store):
        events = [
            {
                "pr_number": 1,
                "pr_url": "https://x/pr/1",
                "comment_path": "a.py",
                "comment_line": 10,
                "comment_category": "bug",
                "comment_severity": "warning",
                "comment_title": "t",
                "signal": "accepted",
                "actor": "u1",
            },
            {
                "pr_number": 1,
                "pr_url": "https://x/pr/1",
                "comment_path": "b.py",
                "comment_line": 5,
                "comment_category": "security",
                "comment_severity": "blocker",
                "comment_title": "t2",
                "signal": "accepted",
                "actor": "u1",
            },
        ]
        n = store.record_bulk_feedback(events)
        assert n == 2
        listed = store.list_feedback()
        assert len(listed) == 2
        assert {e.comment_path for e in listed} == {"a.py", "b.py"}

    def test_empty_list_returns_zero(self, store):
        assert store.record_bulk_feedback([]) == 0


# ── synthesize_rules (accept/reject logic) ──


def _fb(
    store: IndexStore,
    *,
    signal: str,
    category: str,
    path: str = "src/auth.py",
    pr: int = 1,
) -> None:
    store.record_feedback(
        pr_number=pr,
        pr_url=f"https://x/pr/{pr}",
        comment_path=path,
        comment_line=1,
        comment_category=category,
        comment_severity="",
        comment_title="",
        signal=signal,
        actor="tester",
    )


class TestSynthesizeRules:
    def test_no_events_no_rules(self, store):
        assert synthesize_rules(store) == 0

    def test_category_wide_reject_rule(self, store):
        for i in range(5):
            _fb(store, signal="rejected", category="style", pr=i)
        n = synthesize_rules(store)
        assert n >= 1
        rules = store.list_learned_rules()
        assert any(r.source_signal == "reject_pattern" for r in rules)

    def test_high_accept_rate_does_not_create_accept_pattern(self, store):
        # Accept signals are recorded for analytics but no longer become rules.
        for i in range(5):
            _fb(store, signal="accepted", category="bug", pr=i)
        n = synthesize_rules(store)
        assert n == 0
        rules = store.list_learned_rules()
        assert not any(r.source_signal == "accept_pattern" for r in rules)

    def test_reject_pattern_still_created_with_accepts_present(self, store):
        for i in range(5):
            _fb(store, signal="rejected", category="style", pr=i)
        for i in range(5, 15):
            _fb(store, signal="accepted", category="style", pr=i)
        synthesize_rules(store)
        rules = store.list_learned_rules()
        style_rules = [r for r in rules if r.category == "style"]
        assert style_rules
        assert all(r.source_signal == "reject_pattern" for r in style_rules)

    def test_ignores_unknown_category(self, store):
        for i in range(5):
            _fb(store, signal="rejected", category="", pr=i)
        assert synthesize_rules(store) == 0

    def test_reject_pattern_stores_evidence_prs(self, store):
        for i in range(5):
            _fb(store, signal="rejected", category="style", pr=10 + i)
        assert synthesize_rules(store) >= 1
        rules = [r for r in store.list_learned_rules() if r.source_signal == "reject_pattern"]
        assert rules
        evidence = {n for r in rules for n in (r.evidence_prs or "").split(",") if n}
        assert evidence >= {"10", "11", "12", "13", "14"}


    def test_reject_pattern_is_titled_for_inject(self, store):
        from mira.analysis.learned_rules import select_learned_rules, unpack_learned_rule

        for i in range(5):
            _fb(store, signal="rejected", category="style", pr=i)
        assert synthesize_rules(store) >= 1
        rules = [r for r in store.list_learned_rules() if r.source_signal == "reject_pattern"]
        assert rules
        title, body = unpack_learned_rule(rules[0].rule_text)
        assert title.startswith("Raise the bar on")
        assert body
        injected = select_learned_rules(rules, ["src/auth.py"])
        assert injected
        assert injected[0]["title"] == title


class TestLlmSynthCooldown:
    def test_elapsed_when_never_ran(self, store):
        assert llm_synth_cooldown_elapsed(store) is True

    def test_blocked_immediately_after_mark(self, store):
        mark_llm_synth(store)
        last = store.last_feedback_signal_at("learn_synth")
        assert last > 0
        assert llm_synth_cooldown_elapsed(store, now=last + 10) is False
        assert llm_synth_cooldown_elapsed(store, now=last + 4000) is True


# ── synthesize_from_human_reviews (LLM) ──


class TestSynthesizeFromHumanReviews:
    @pytest.mark.asyncio
    async def test_returns_zero_when_too_few_comments(self, store):
        llm = AsyncMock()
        _fb(store, signal="human_review", category="human_review")
        n = await synthesize_from_human_reviews(store, llm)
        assert n == 0
        llm.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_llm_and_stores_rules(self, store):
        # Seed with 3 human-review events
        for i in range(3):
            store.record_feedback(
                pr_number=i + 1,
                pr_url=f"https://x/{i + 1}",
                comment_path=f"src/f{i}.py",
                comment_line=10,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"Reviewer said please no raw sql #{i}",
                signal="human_review",
                actor="alice",
            )

        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {
                "actions": [
                    _create(
                        "Flag raw SQL queries outside the data layer",
                        evidence_count=3,
                        prs=[1, 2, 99],
                    ),
                    _create(
                        "Prefer async/await over callback patterns",
                        evidence_count=2,
                        prs=[3],
                    ),
                ]
            }
        )

        n = await synthesize_from_human_reviews(store, llm)
        assert n == 2
        assert llm.complete.await_count >= 2
        # Verify stored as human_pattern source
        rules = store.list_learned_rules()
        human_rules = [r for r in rules if r.source_signal == "human_pattern"]
        assert len(human_rules) == 2
        assert all(r.path_pattern.startswith("__human_") for r in human_rules)
        by_text = {r.rule_text: r for r in human_rules}
        sql_key = next(k for k in by_text if "Flag raw SQL" in k)
        assert by_text[sql_key].evidence_prs == "1,2"
        assert by_text[sql_key].sample_count == 3
        async_key = next(k for k in by_text if "async/await" in k)
        assert by_text[async_key].evidence_prs == "3"
        assert by_text[async_key].sample_count == 2
        # Cluster prompt includes catalog; extract includes PR markers.
        prompts = [c.kwargs["messages"][0]["content"] for c in llm.complete.await_args_list]
        assert any("Existing catalog" in p for p in prompts)
        assert any("PR #" in p for p in prompts)

    @pytest.mark.asyncio
    async def test_on_progress_emits_extract_cluster(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i + 1,
                pr_url=f"https://x/{i + 1}",
                comment_path=f"src/f{i}.py",
                comment_line=10,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"Please avoid bare except #{i}",
                signal="human_review",
                actor="alice",
            )

        phases: list[str] = []

        def on_progress(fields: dict) -> None:
            phase = str(fields.get("phase") or "")
            if phase and (not phases or phases[-1] != phase):
                phases.append(phase)

        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {
                "actions": [
                    {
                        "action": "create",
                        "title": "Catch specific exceptions",
                        "body": "Prefer named exception types over bare except.",
                        "evidence_count": 3,
                        "prs": [1, 2, 3],
                    }
                ]
            }
        )
        n = await synthesize_from_human_reviews(store, llm, on_progress=on_progress)
        assert n == 1
        assert "classify" not in phases
        assert "extract" in phases
        assert "cluster" in phases
        assert phases[-1] == "complete"

    @pytest.mark.asyncio
    async def test_structured_title_body_pack_into_rule_text(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i + 1,
                pr_url=f"https://x/{i + 1}",
                comment_path=f"src/f{i}.py",
                comment_line=10,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"please use transactions for multi writes #{i}",
                signal="human_review",
                actor="alice",
            )
        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {
                "actions": [
                    {
                        "action": "create",
                        "title": "Wrap multi-write requests in a transaction",
                        "body": (
                            "Wrap related DB writes in a single transaction instead of "
                            "partial commits."
                        ),
                        "evidence_count": 3,
                        "prs": [1, 2],
                    },
                    {
                        "action": "create",
                        "title": "Use camelCase for consistency",
                        "body": "Prefer camelCase.",
                        "evidence_count": 2,
                    },
                ]
            }
        )
        n = await synthesize_from_human_reviews(store, llm)
        # Soft conventions are kept (admins filter); both creates land.
        assert n == 2
        human = [r for r in store.list_learned_rules() if r.source_signal == "human_pattern"]
        assert len(human) == 2
        texts = "\n".join(r.rule_text for r in human)
        assert "Wrap multi-write requests in a transaction" in texts
        assert "partial commits" in texts
        assert "Use camelCase for consistency" in texts
        prompts = [c.kwargs["messages"][0]["content"] for c in llm.complete.await_args_list]
        assert any('"title"' in p for p in prompts)
        assert any('"body"' in p for p in prompts)

    @pytest.mark.asyncio
    async def test_new_synth_adds_rules_without_overwriting_approved(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path=f"src/f{i}.py",
                comment_line=10,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"note {i}",
                signal="human_review",
                actor="alice",
            )
        # Approved rule must stay untouched.
        approved = store.upsert_learned_rule(
            rule_text=pack_learned_rule(
                "Keep approved text",
                "Frozen approved learning.",
            ),
            source_signal="human_pattern",
            category="human_review",
            path_pattern="__human_approved__",
            sample_count=10,
            status="approved",
        )
        assert approved.status == "approved"

        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {
                "actions": [
                    _create(
                        "Brand new research finding about logging",
                        evidence_count=4,
                    ),
                    _create(
                        "Another distinct finding about retries",
                        evidence_count=3,
                    ),
                ]
            }
        )
        n = await synthesize_from_human_reviews(store, llm)
        assert n == 2

        rules = store.list_learned_rules()
        human = [r for r in rules if r.source_signal == "human_pattern"]
        assert len(human) == 3
        kept = store.get_learned_rule(approved.id)
        assert kept is not None
        assert "Keep approved text" in kept.rule_text
        assert kept.status == "approved"
        pending = [r for r in human if r.status == "pending"]
        assert len(pending) == 2
        assert all(r.path_pattern.startswith("__human_") for r in pending)
        prompts = [c.kwargs["messages"][0]["content"] for c in llm.complete.await_args_list]
        assert any(f"id={approved.id}" in p for p in prompts)

    @pytest.mark.asyncio
    async def test_same_wording_reuses_row_and_bumps_samples(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"c{i}",
                signal="human_review",
                actor="u",
            )
        payload = {
            "actions": [
                _create(
                    "Prefer named constants over magic numbers",
                    evidence_count=2,
                )
            ]
        }
        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(payload)
        assert await synthesize_from_human_reviews(store, llm) == 1
        first = [r for r in store.list_learned_rules() if r.source_signal == "human_pattern"]
        assert len(first) == 1
        path = first[0].path_pattern
        llm.complete.side_effect = _staged_llm_complete(payload)
        assert await synthesize_from_human_reviews(store, llm) == 1
        again = [r for r in store.list_learned_rules() if r.source_signal == "human_pattern"]
        assert len(again) == 1
        assert again[0].path_pattern == path

    @pytest.mark.asyncio
    async def test_merge_bumps_approved_without_rewriting_text(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"c{i}",
                signal="human_review",
                actor="u",
            )
        approved = store.upsert_learned_rule(
            rule_text=pack_learned_rule(
                "Always validate auth tokens at the edge",
                "Check tokens at the edge.",
            ),
            source_signal="human_pattern",
            category="human_review",
            path_pattern="__human_abc__",
            sample_count=2,
            status="approved",
        )
        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {
                "actions": [
                    _merge(
                        approved.id,
                        title="Should not overwrite approved text",
                        body="This rewrite must not apply.",
                        evidence_count=9,
                    )
                ]
            }
        )
        assert await synthesize_from_human_reviews(store, llm) == 1
        kept = store.get_learned_rule(approved.id)
        assert kept is not None
        assert "Always validate auth tokens at the edge" in kept.rule_text
        assert kept.sample_count == 9
        assert kept.status == "approved"

    @pytest.mark.asyncio
    async def test_merge_can_rewrite_pending_text(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"c{i}",
                signal="human_review",
                actor="u",
            )
        pending = store.upsert_learned_rule(
            rule_text=pack_learned_rule(
                "Old pending wording",
                "Temporary pending text.",
            ),
            source_signal="human_pattern",
            category="human_review",
            path_pattern="__human_pending__",
            sample_count=1,
            status="pending",
        )
        improved = pack_learned_rule(
                "Improved pending wording",
                "Sharper pending text.",
            )
        title, body = unpack_learned_rule(improved)
        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {
                "actions": [
                    _merge(
                        pending.id,
                        title=title,
                        body=body,
                        evidence_count=4,
                    )
                ]
            }
        )
        assert await synthesize_from_human_reviews(store, llm) == 1
        got = store.get_learned_rule(pending.id)
        assert got is not None
        assert got.rule_text == improved
        assert got.sample_count == 4
        assert got.status == "pending"

    @pytest.mark.asyncio
    async def test_skip_actions_do_not_upsert(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"c{i}",
                signal="human_review",
                actor="u",
            )
        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {"actions": [{"action": "skip", "note": "already covered"}]}
        )
        assert await synthesize_from_human_reviews(store, llm) == 0
        assert store.list_learned_rules() == []

    @pytest.mark.asyncio
    async def test_replace_pending_clears_auto_synth_keeps_remember(self, store):
        from mira.analysis.feedback import clear_pending_synth_rules

        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"c{i}",
                signal="human_review",
                actor="u",
            )
        auto = store.upsert_learned_rule(
            rule_text="Auto pending mush.",
            source_signal="human_pattern",
            category="human_review",
            path_pattern="__human_auto__",
            sample_count=1,
            status="pending",
        )
        remember = store.create_learned_rule(
            rule_text="Hand remember rule.",
            source_signal="human_pattern",
            category="other",
            path_pattern="",
            sample_count=1,
            status="pending",
            created_by="alice",
        )
        cleared = clear_pending_synth_rules(store)
        assert cleared == 1
        assert store.get_learned_rule(auto.id) is None
        assert store.get_learned_rule(remember.id) is not None

        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {
                "actions": [
                    _create(
                        "Flag bare except",
                        body="Catch named exceptions instead of bare except.",
                        evidence_count=2,
                    )
                ]
            }
        )
        n = await synthesize_from_human_reviews(store, llm, replace_pending=True)
        assert n == 1
        assert store.get_learned_rule(remember.id) is not None
        human = [r for r in store.list_learned_rules() if r.source_signal == "human_pattern"]
        assert any("Flag bare except" in r.rule_text for r in human)

    @pytest.mark.asyncio
    async def test_near_rejected_create_is_skipped(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"c{i}",
                signal="human_review",
                actor="u",
            )
        rejected = store.upsert_learned_rule(
            rule_text=pack_learned_rule(
                "Flag bare except clauses",
                "Catch named exceptions.",
            ),
            source_signal="human_pattern",
            category="human_review",
            path_pattern="__human_rej__",
            sample_count=1,
            status="rejected",
        )
        assert rejected.status == "rejected"
        llm = AsyncMock()
        llm.complete.side_effect = _staged_llm_complete(
            {
                "actions": [
                    _create(
                        "Flag bare except clauses",
                        body="Catch named exceptions.",
                        evidence_count=3,
                    )
                ]
            }
        )
        assert await synthesize_from_human_reviews(store, llm) == 0
        pending = [r for r in store.list_learned_rules() if r.status == "pending"]
        assert pending == []

    @pytest.mark.asyncio
    async def test_bad_json_returns_zero(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title="b",
                signal="human_review",
                actor="u",
            )
        llm = AsyncMock()
        # Extract returns bad JSON → zero upserts.
        llm.complete.return_value = "this is not json"
        assert await synthesize_from_human_reviews(store, llm) == 0

    @pytest.mark.asyncio
    async def test_llm_failure_returns_zero(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title="b",
                signal="human_review",
                actor="u",
            )
        llm = AsyncMock()
        llm.complete.side_effect = RuntimeError("api down")
        assert await synthesize_from_human_reviews(store, llm) == 0


# ── Webhook routing ──


def test_webhook_routes_merged_pr(monkeypatch):
    from fastapi.testclient import TestClient

    from mira.platforms import server as wh

    called_with: dict = {}

    async def fake_handler(payload, app_auth, bot_name):
        called_with["payload"] = payload
        called_with["bot_name"] = bot_name

    monkeypatch.setattr("mira.platforms.github.webhook.handle_pr_merged", fake_handler)

    app_auth = object()
    app = wh.create_app(app_auth, webhook_secret="secret", bot_name="mira")

    payload = {
        "action": "closed",
        "installation": {"id": 42},
        "sender": {"login": "someone"},
        "pull_request": {
            "number": 7,
            "merged": True,
            "title": "Fix it",
            "body": "",
            "base": {"ref": "main"},
            "head": {"ref": "f"},
            "labels": [],
        },
        "repository": {"owner": {"login": "acme"}, "name": "web"},
    }

    import hashlib
    import hmac

    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    client = TestClient(app)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "processing"}
    assert called_with["bot_name"] == "mira"


def test_webhook_ignores_closed_but_not_merged(monkeypatch):
    from fastapi.testclient import TestClient

    from mira.platforms import server as wh

    called = []

    async def fake_handler(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr("mira.platforms.github.webhook.handle_pr_merged", fake_handler)

    app_auth = object()
    app = wh.create_app(app_auth, webhook_secret="secret", bot_name="mira")

    payload = {
        "action": "closed",
        "installation": {"id": 42},
        "sender": {"login": "someone"},
        "pull_request": {
            "number": 7,
            "merged": False,
            "title": "Abandoned",
            "body": "",
            "base": {"ref": "main"},
            "head": {"ref": "f"},
            "labels": [],
        },
        "repository": {"owner": {"login": "acme"}, "name": "web"},
    }

    import hashlib
    import hmac

    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    client = TestClient(app)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert called == []


# ── End-to-end handler test ──


class _StoreProxy:
    """Wraps an IndexStore so the handler's store.close() doesn't close the
    shared fixture store — the test still needs to read from it afterwards."""

    def __init__(self, inner: IndexStore) -> None:
        self._inner = inner

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_handle_pr_merged_end_to_end(tmp_path, monkeypatch):
    """Exercise the whole handler: provider → parser → store → synthesis chain."""
    from mira.platforms.github import webhook as handlers

    store = IndexStore(str(tmp_path / "test.db"))

    # Seed a prior rejected event — the corresponding bot thread must NOT be
    # re-recorded as accepted on merge.
    store.record_feedback(
        pr_number=42,
        pr_url="https://github.com/acme/web/pull/42",
        comment_path="src/db.py",
        comment_line=20,
        comment_category="",
        comment_severity="",
        comment_title="",
        signal="rejected",
        actor="alice",
    )

    monkeypatch.setattr(
        "mira.platforms.handlers._open_store",
        lambda owner, repo, platform="github": _StoreProxy(store),
    )

    mock_provider = MagicMock()
    mock_provider.get_all_bot_threads = AsyncMock(
        return_value=[
            BotThreadRecord(
                thread_id="t1",
                path="src/auth.py",
                line=10,
                body=(
                    "🐛 **Bug**\n"
                    "🛑 Blocker — must fix before merge\n"
                    "\n"
                    "**Null handling missing**\n"
                    "\n"
                    "The function crashes on empty input.\n"
                ),
                is_resolved=False,
            ),
            # At the rejected location — should be skipped.
            BotThreadRecord(
                thread_id="t2",
                path="src/db.py",
                line=20,
                body="🔒 **Security issue**\n⚠️ Warning\n\n**Raw SQL**\n\nbody",
                is_resolved=True,
            ),
            BotThreadRecord(
                thread_id="t3",
                path="src/utils.py",
                line=5,
                body="⚡ **Performance**\n\n**Slow loop**\n\nbody",
                is_resolved=False,
            ),
            # Malformed body — should be skipped (no parseable category).
            BotThreadRecord(
                thread_id="t4",
                path="src/other.py",
                line=1,
                body="not a structured comment",
                is_resolved=False,
            ),
        ]
    )
    mock_provider.get_human_review_comments = AsyncMock(
        return_value=[
            HumanReviewComment(
                path="src/auth.py",
                line=10,
                body="Please avoid raw SQL here — use the query builder.",
                author="reviewer1",
            ),
            HumanReviewComment(
                path="src/api.py",
                line=5,
                body="Let's add proper error handling to this request path.",
                author="reviewer2",
            ),
            # Empty body — should be skipped.
            HumanReviewComment(path="x.py", line=1, body="   ", author="reviewer3"),
        ]
    )

    monkeypatch.setattr(handlers, "create_provider", lambda *a, **kw: mock_provider)

    app_auth = MagicMock()
    app_auth.get_installation_token = AsyncMock(return_value="test-token")

    fake_config = MagicMock()
    fake_config.llm = MagicMock()
    monkeypatch.setattr("mira.platforms.handlers.load_config", lambda: fake_config)

    # Bypass the dashboard DB lookup in llm_config_for.
    import mira.dashboard.models_config as mc

    monkeypatch.setattr(mc, "llm_config_for", lambda purpose, base: base)

    fake_llm = MagicMock()
    fake_llm.complete = AsyncMock(
        side_effect=_staged_llm_complete(
            {
                "actions": [
                    _create(
                        "Avoid raw SQL queries outside the data-access layer",
                        evidence_count=2,
                    ),
                ]
            }
        )
    )
    monkeypatch.setattr("mira.platforms.handlers.create_llm", lambda *a, **kw: fake_llm)

    payload = {
        "action": "closed",
        "installation": {"id": 123},
        "sender": {"login": "alice"},
        "pull_request": {
            "number": 42,
            "merged": True,
            "merged_by": {"login": "alice"},
            "title": "Add auth",
            "body": "",
            "base": {"ref": "main"},
            "head": {"ref": "f"},
            "labels": [],
        },
        "repository": {"owner": {"login": "acme"}, "name": "web"},
    }

    await handlers.handle_pr_merged(payload, app_auth, "mira")

    events = store.list_feedback(limit=100)
    by_signal: dict[str, list] = {"accepted": [], "human_review": [], "rejected": []}
    for e in events:
        by_signal.setdefault(e.signal, []).append(e)

    # Bot threads: t1 (accepted), t2 (skipped — prior reject), t3 (accepted),
    # t4 (skipped — no parseable metadata).
    assert len(by_signal["accepted"]) == 2
    paths = {e.comment_path for e in by_signal["accepted"]}
    assert paths == {"src/auth.py", "src/utils.py"}
    cats = {e.comment_category for e in by_signal["accepted"]}
    assert cats == {"bug", "performance"}

    # Titles parsed too.
    titles = {e.comment_title for e in by_signal["accepted"]}
    assert "Null handling missing" in titles
    assert "Slow loop" in titles

    # Two human-review events (third had empty body).
    assert len(by_signal["human_review"]) == 2
    human_paths = {e.comment_path for e in by_signal["human_review"]}
    assert human_paths == {"src/auth.py", "src/api.py"}

    # Original rejected event still present and not duplicated.
    assert len(by_signal["rejected"]) == 1

    # LLM was invoked for staged synth (extract + cluster).
    assert fake_llm.complete.await_count >= 2

    # One human_pattern rule stored.
    rules = store.list_learned_rules()
    human_rules = [r for r in rules if r.source_signal == "human_pattern"]
    assert len(human_rules) == 1
    assert "raw sql" in human_rules[0].rule_text.lower()

    # ── Dedup on retry ──
    fake_llm.complete.reset_mock()
    mock_provider.get_all_bot_threads.reset_mock()
    mock_provider.get_human_review_comments.reset_mock()

    await handlers.handle_pr_merged(payload, app_auth, "mira")

    # No new events should have been recorded.
    events_after = store.list_feedback(limit=100)
    assert len(events_after) == len(events)

    # Provider fetch methods should not have been called (early return).
    mock_provider.get_all_bot_threads.assert_not_called()
    mock_provider.get_human_review_comments.assert_not_called()
    fake_llm.complete.assert_not_called()

    store.close()
