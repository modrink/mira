"""Platform-neutral webhook handlers — shared by the GitHub and GitLab
webhook layers. Each takes a provider/auth and operates through the engine;
none is tied to a specific platform's payload shape."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.core.review_status import tracker as review_tracker
from mira.dashboard.models_config import llm_config_for
from mira.index.store import IndexStore
from mira.llm import create_llm
from mira.llm.prompts.review import build_conversation_prompt
from mira.llm.tool_schemas import SUBMIT_THREAD_REPLY_TOOL
from mira.llm.utils import strip_code_fences, strip_think_blocks

logger = logging.getLogger(__name__)

_REVIEW_KEYWORDS = {"review", "review this", "review this pr"}

_REJECT_KEYWORDS = {"reject", "dismiss", "resolve", "ignore"}

_REVIEW_REST_KEYWORDS = {"review-rest", "review rest", "rest", "continue"}

_HELP_KEYWORDS = {"help", "?", "commands"}

_THREAD_REPLY_ENV = Environment(
    loader=FileSystemLoader(
        str(Path(__file__).resolve().parents[1] / "llm" / "prompts" / "templates")
    ),
    trim_blocks=True,
    lstrip_blocks=True,
)

_THREAD_REPLY_TEMPLATE = _THREAD_REPLY_ENV.get_template("thread_reply.jinja2")

PAUSE_LABEL = "mira-paused"

_PAUSE_KEYWORDS = {"pause"}

_RESUME_KEYWORDS = {"resume"}

_REMEMBER_PREFIX = "remember"


def _open_store(owner: str, repo: str, platform: str = "github") -> IndexStore:
    """Open an IndexStore for the given owner/repo."""
    return IndexStore.open(owner, repo, platform=platform)


def _help_message(bot_name: str) -> str:
    """Markdown help comment listing every command Mira understands."""
    return (
        f"### Mira commands\n\n"
        f"Mention `@{bot_name}` in a PR comment followed by one of these verbs:\n\n"
        f"| Command | What it does |\n"
        f"|---|---|\n"
        f"| `@{bot_name} review` | Re-run the full review on this PR. Useful after force-pushes or when you want a fresh pass. |\n"
        f"| `@{bot_name} review-rest` | Review files that were skipped on the first pass because the PR was too large. Aliases: `rest`, `continue`. |\n"
        f"| `@{bot_name} remember <rule>` | Capture a team preference as a pending learning for admin approval. |\n"
        f"| `@{bot_name} pause` | Pause Mira on this PR. No more reviews until you resume. Adds a `mira-paused` label. |\n"
        f"| `@{bot_name} resume` | Resume Mira on a paused PR and re-review the latest diff. |\n"
        f"| `@{bot_name} help` | Show this message. Aliases: `?`, `commands`. |\n"
        f"| `@{bot_name} <anything else>` | Ask a free-form question about the PR. Mira will reply inline using the PR diff as context. |\n\n"
        f"On an inline review comment Mira posted, reply with `@{bot_name} reject` "
        f"(aliases: `dismiss`, `resolve`, `ignore`) to mark the thread resolved and "
        f"teach Mira not to make similar suggestions in the future.\n\n"
        f"To skip a PR entirely, include `@{bot_name} ignore` in the PR body.\n\n"
        f"Full docs: https://docs.miracode.ai/commands"
    )


async def run_pr_review(
    provider: Any,
    owner: str,
    repo: str,
    number: int,
    pr_url: str,
    is_private: bool,
    bot_name: str,
    platform: str = "github",
    pr_title: str = "",
) -> None:
    """Platform-neutral review core: review a PR/MR and post the result.

    Shared by the GitHub and GitLab webhook handlers — everything here goes
    through the ``provider`` abstraction and the engine, so it's the same for
    every platform.
    """
    repo_full = f"{owner}/{repo}"

    # Atomically claim the slot — avoids stacking redundant runs when
    # two concurrent webhooks arrive. Returns False if already reviewing.
    if not review_tracker.try_start(repo_full, number, pr_title, pr_url):
        logger.info("Review already in progress for %s, skipping", pr_url)
        return

    config = load_config()
    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("review", config.llm))
    indexing_llm = create_llm(llm_config_for("indexing", config.llm))
    engine = ReviewEngine(
        config=config,
        llm=llm,
        provider=provider,
        bot_name=bot_name,
        indexing_llm=indexing_llm,
    )

    from mira.dashboard.api import _app_db

    # Keep visibility current — the blast-radius filter relies on it to avoid
    # naming private repos in a public repo's review.
    _app_db.set_repo_visibility(owner, repo, is_private, platform=platform)

    repo_record = _app_db.get_repo(owner, repo, platform=platform)
    is_indexed = bool(repo_record and repo_record.status == "ready")

    logger.info("Reviewing %s (indexed=%s)", pr_url, is_indexed)
    try:
        result = await engine.review_pr(pr_url)
        review_tracker.complete(repo_full, number)
    except Exception as exc:
        review_tracker.fail(repo_full, number, str(exc))
        raise

    # The walkthrough comment already carries the "more accurate after indexing"
    # nudge for unindexed repos, so we don't post a separate note here — that
    # would repeat on every push.

    logger.info("Review complete for %s", pr_url)

    from mira.models import Severity, build_review_stats
    from mira.outbound_webhooks import (
        REVIEW_COMPLETED,
        REVIEW_HIGH_SEVERITY,
        dispatch_event,
    )

    stats = build_review_stats(result.comments)
    event_data = {
        "repo": repo_full,
        "pr_url": pr_url,
        "number": number,
        "comments": len(result.comments),
        "key_issues": len(result.key_issues),
        "severities": {sev.name.lower(): n for sev, n in stats.items()},
    }
    await dispatch_event(REVIEW_COMPLETED, event_data)
    if any(sev >= Severity.WARNING for sev in stats):
        await dispatch_event(REVIEW_HIGH_SEVERITY, event_data)


async def run_pr_command(
    provider: Any,
    owner: str,
    repo: str,
    number: int,
    pr_url: str,
    question: str,
    actor: str,
    bot_name: str,
    platform: str = "github",
    pr_title: str = "",
) -> None:
    """Platform-neutral handler for an @-mention command on a PR/MR.

    Dispatches help / review / review-rest / free-form Q&A through the provider
    and engine. Shared by the GitHub and GitLab comment handlers.
    """
    repo_full = f"{owner}/{repo}"
    config = load_config()
    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("review", config.llm))
    indexing_llm = create_llm(llm_config_for("indexing", config.llm))

    normalized = question.lower().strip()
    is_review = normalized in _REVIEW_KEYWORDS
    is_review_rest = normalized in _REVIEW_REST_KEYWORDS
    is_help = normalized in _HELP_KEYWORDS
    remember_body = ""
    if normalized == _REMEMBER_PREFIX or normalized.startswith(_REMEMBER_PREFIX + " "):
        remember_body = question.strip()[len(_REMEMBER_PREFIX) :].strip()

    if is_help:
        pr_info_for_help = await provider.get_pr_info(pr_url)
        await provider.post_comment(pr_info_for_help, _help_message(bot_name))
        logger.info("Help requested on %s by @%s", pr_url, actor)
        return

    if remember_body or normalized == _REMEMBER_PREFIX:
        pr_info = await provider.get_pr_info(pr_url)
        if not remember_body:
            await provider.post_comment(
                pr_info,
                f"> @{actor}: usage — `@{bot_name} remember <team preference>`",
            )
            return
        from mira.analysis.learned_rules import find_near_duplicate_rule, human_pattern_key

        store = _open_store(owner, repo, platform)
        try:
            catalog = [r for r in store.list_learned_rules() if r.status != "rejected"]
            near = find_near_duplicate_rule(remember_body, catalog)
            if near is not None:
                store.bump_learned_rule_evidence(
                    near.id,
                    max(1, near.sample_count) + 1,
                    evidence_prs=str(number) if number else "",
                )
                where = "Rules → Pending" if near.status == "pending" else "Rules → Active"
                await provider.post_comment(
                    pr_info,
                    f"> @{actor}: reinforced an existing **{near.status}** learning "
                    f"(near-duplicate) — see {where}.",
                )
                logger.info(
                    "Remember near-dupe on %s by @%s → rule %d",
                    pr_url,
                    actor,
                    near.id,
                )
                return
            row = store.create_learned_rule(
                rule_text=remember_body[:2000],
                category="human_review",
                path_pattern=human_pattern_key(remember_body),
                source_signal="manual",
                status="pending",
                active=True,
                created_by=actor,
            )
            if number and row is not None:
                store.bump_learned_rule_evidence(
                    row.id, max(1, row.sample_count), evidence_prs=str(number)
                )
        finally:
            store.close()
        await provider.post_comment(
            pr_info,
            f"> @{actor}: remembered as a **pending** learning — an admin can approve "
            "it on the Rules → Pending tab so it injects into future reviews.",
        )
        logger.info("Remember on %s by @%s (%d chars)", pr_url, actor, len(remember_body))
        return

    if is_review_rest:
        from mira.dashboard.api import _app_db

        progress = _app_db.get_pr_review_progress(owner, repo, number, platform=platform)
        if not progress or not progress.skipped_paths:
            pr_info_for_reply = await provider.get_pr_info(pr_url)
            await provider.post_comment(
                pr_info_for_reply,
                f"> @{actor}: nothing left to review — every file in this "
                "PR has already been covered. 🎉",
            )
            return
        engine = ReviewEngine(
            config=config,
            llm=llm,
            provider=provider,
            bot_name=bot_name,
            indexing_llm=indexing_llm,
        )
        engine._review_only_paths = set(progress.skipped_paths)  # type: ignore[attr-defined]
        if not review_tracker.try_start(repo_full, number, pr_title, pr_url):
            logger.info("Review already in progress for %s, skipping", pr_url)
            return
        logger.info(
            "review-rest on %s by @%s — %d file(s)", pr_url, actor, len(progress.skipped_paths)
        )
        try:
            await engine.review_pr(pr_url)
            review_tracker.complete(repo_full, number)
        except Exception as exc:
            review_tracker.fail(repo_full, number, str(exc))
            raise
    elif is_review:
        engine = ReviewEngine(
            config=config,
            llm=llm,
            provider=provider,
            bot_name=bot_name,
            indexing_llm=indexing_llm,
        )
        if not review_tracker.try_start(repo_full, number, pr_title, pr_url):
            logger.info("Review already in progress for %s, skipping", pr_url)
            return
        logger.info("Re-review triggered for %s by @%s", pr_url, actor)
        try:
            await engine.review_pr(pr_url)
            review_tracker.complete(repo_full, number)
        except Exception as exc:
            review_tracker.fail(repo_full, number, str(exc))
            raise
    else:
        pr_info = await provider.get_pr_info(pr_url)
        diff_text = await provider.get_pr_diff(pr_info)
        messages = build_conversation_prompt(
            question=question,
            diff_text=diff_text,
            pr_title=pr_info.title,
            pr_description=pr_info.description,
        )
        response = await llm.complete(messages, json_mode=False)
        await provider.post_comment(pr_info, f"> @{actor} asked: {question}\n\n{response}")
        logger.info("Replied to comment on %s", pr_url)


async def run_thread_reply(
    provider: Any,
    pr_info: Any,
    human_reply: str,
    comment_id: int,
    *,
    original_suggestion: str = "",
    thread_id: str | None = None,
    comment_node_id: str | None = None,
    comment_path: str = "",
    comment_line: int = 0,
    actor: str = "",
    bot_name: str = "miracodeai",
    platform: str = "github",
) -> None:
    """Platform-neutral free-form thread reply with intent classification.

    The LLM classifies the human's message and we respond accordingly:
    ``disagreement`` → reply + resolve the thread + record a ``rejected``
    feedback signal (same learning signal as an explicit reject); ``question``
    → answer, leave open; ``agreement`` / ``other`` → acknowledge, leave open.
    """
    config = load_config()
    llm = create_llm(llm_config_for("indexing", config.llm))
    prompt = _THREAD_REPLY_TEMPLATE.render(
        user_reply=human_reply or "(empty)",
        original_suggestion=original_suggestion,
    )
    # Tool calling forces a schema-valid result — more reliable than parsing
    # free-form JSON. The provider's tenacity decorator retries transient fails.
    try:
        raw = await llm.complete_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[SUBMIT_THREAD_REPLY_TOOL],
            temperature=0.0,
        )
        data = json.loads(strip_think_blocks(strip_code_fences(raw))) if raw else {}
    except Exception as exc:
        logger.warning("Free-form thread reply LLM call failed: %s", exc)
        return

    intent = str(data.get("intent", "other")).lower()
    reply_text = str(data.get("reply", "")).strip()
    if not reply_text:
        logger.warning("Free-form thread reply: empty reply (intent=%s). Skipping.", intent)
        return

    try:
        await provider.reply_to_review_comment(pr_info, comment_id, reply_text)
    except Exception as exc:
        logger.warning("Failed to post thread reply: %s", exc)
        return

    if intent == "disagreement":
        try:
            tid = thread_id
            if tid is None and comment_node_id:
                tid = await provider.get_thread_id_for_comment(comment_node_id, pr_info)
            if tid:
                await provider.resolve_threads(pr_info, [tid])
        except Exception as exc:
            logger.warning("Failed to resolve disagreement thread: %s", exc)
        try:
            store = _open_store(pr_info.owner, pr_info.repo, platform)
            try:
                store.record_feedback(
                    pr_number=pr_info.number,
                    pr_url=pr_info.url,
                    comment_path=comment_path,
                    comment_line=comment_line,
                    comment_category="",
                    comment_severity="",
                    comment_title="",
                    signal="rejected",
                    actor=actor,
                )
            finally:
                store.close()
        except Exception as fb_err:
            logger.debug("Failed to record disagreement feedback: %s", fb_err)

    logger.info("Thread reply (%s) on %s: %s", intent, pr_info.url, reply_text[:80])


async def run_pr_merged_learning(
    provider: Any,
    pr_info: Any,
    bot_name: str,
    merged_by: str,
    platform: str = "github",
    *,
    synthesize: bool = True,
) -> dict[str, int]:
    """Platform-neutral merge-time learning: record accept/reject + human-review
    signals and (optionally) synthesize rules. Shared by GitHub and GitLab.

    ``synthesize=False`` records feedback events only — used by historical
    backfill so synthesis runs once per repo at the end instead of per PR.
    Returns counts: accepted, human_recorded, deterministic_rules, llm_rules,
    skipped (1 if this PR was already processed).
    """
    from mira.providers.formatting import parse_bot_comment_metadata

    owner, repo, number, pr_url = pr_info.owner, pr_info.repo, pr_info.number, pr_info.url
    store = _open_store(owner, repo, platform)
    accepted = 0
    human_recorded = 0
    deterministic_rules = 0
    llm_rules = 0
    try:
        if store.has_pr_merge_learning(number):
            logger.info("PR %s already processed for merge-time learning", pr_url)
            return {
                "accepted": 0,
                "human_recorded": 0,
                "deterministic_rules": 0,
                "llm_rules": 0,
                "skipped": 1,
            }
        rejected_locations = {
            (e.comment_path, e.comment_line)
            for e in store.list_feedback_for_pr(number)
            if e.signal == "rejected"
        }

        try:
            bot_threads = await provider.get_all_bot_threads(pr_info)
        except Exception as exc:
            logger.warning("Failed to fetch bot threads for %s: %s", pr_url, exc)
            bot_threads = []

        bot_events: list[dict] = []
        for thread in bot_threads:
            if (thread.path, thread.line) in rejected_locations:
                continue
            meta = parse_bot_comment_metadata(thread.body)
            if not meta["category"]:
                continue
            bot_events.append(
                {
                    "pr_number": number,
                    "pr_url": pr_url,
                    "comment_path": thread.path,
                    "comment_line": thread.line,
                    "comment_category": meta["category"],
                    "comment_severity": meta["severity"],
                    "comment_title": meta["title"],
                    "signal": "accepted",
                    "actor": merged_by,
                    "pr_author": pr_info.author,
                }
            )

        try:
            human_comments = await provider.get_human_review_comments(pr_info, bot_name)
        except Exception as exc:
            logger.warning("Failed to fetch human review comments for %s: %s", pr_url, exc)
            human_comments = []

        human_events: list[dict] = []
        from mira.analysis.feedback import pack_human_comment_for_learning

        for hc in human_comments:
            body = (hc.body or "").strip()
            if not body:
                continue
            human_events.append(
                {
                    "pr_number": number,
                    "pr_url": pr_url,
                    "comment_path": hc.path,
                    "comment_line": hc.line,
                    "comment_category": "human_review",
                    "comment_severity": "",
                    "comment_title": pack_human_comment_for_learning(
                        body, getattr(hc, "diff_hunk", "") or ""
                    ),
                    "signal": "human_review",
                    "actor": hc.author,
                    "pr_author": pr_info.author,
                }
            )

        if bot_events:
            store.record_bulk_feedback(bot_events)
            accepted = len(bot_events)
        if human_events:
            store.record_bulk_feedback(human_events)
            human_recorded = len(human_events)
        if not bot_events and not human_events:
            # Marker so empty PRs stay skipped on re-run (no unique event keys).
            store.record_feedback(
                pr_number=number,
                pr_url=pr_url,
                comment_path="",
                comment_line=0,
                comment_category="",
                comment_severity="",
                comment_title="",
                signal="merge_scanned",
                actor=merged_by,
            )

        if synthesize:
            deterministic_rules, llm_rules = await _synthesize_from_store(
                store, run_llm=human_recorded > 0, force_llm=False
            )
    finally:
        store.close()

    logger.info(
        "PR merged %s: recorded %d accepted + %d human review events; "
        "upserted %d deterministic + %d LLM rules",
        pr_url,
        accepted,
        human_recorded,
        deterministic_rules,
        llm_rules,
    )
    return {
        "accepted": accepted,
        "human_recorded": human_recorded,
        "deterministic_rules": deterministic_rules,
        "llm_rules": llm_rules,
        "skipped": 0,
    }


async def _synthesize_from_store(
    store: Any,
    *,
    run_llm: bool,
    force_llm: bool = False,
    on_progress: Any | None = None,
    replace_pending: bool = False,
) -> tuple[int, int]:
    """Deterministic reject synth + optional catalog-aware LLM synth.

    Live merges debounce LLM calls via ``MIRA_LEARN_SYNTH_COOLDOWN_SEC``.
    Backfill / admin re-synth pass ``force_llm=True`` and ``replace_pending=True``.
    """
    from mira.analysis.feedback import (
        llm_synth_cooldown_elapsed,
        mark_llm_synth,
        synthesize_from_human_reviews,
        synthesize_rules,
    )

    deterministic_rules = synthesize_rules(store)
    llm_rules = 0
    if not run_llm:
        return deterministic_rules, llm_rules
    if not force_llm and not llm_synth_cooldown_elapsed(store):
        logger.info("Skipping LLM learnings synth (cooldown active)")
        return deterministic_rules, llm_rules
    try:
        config = load_config()
        from mira.dashboard.models_config import llm_config_for

        indexing_llm = create_llm(llm_config_for("indexing", config.llm))
        llm_rules = await synthesize_from_human_reviews(
            store,
            indexing_llm,
            on_progress=on_progress,
            replace_pending=replace_pending,
        )
        mark_llm_synth(store)
    except Exception as exc:
        logger.warning("LLM rule synthesis failed: %s", exc)
    return deterministic_rules, llm_rules


async def synthesize_repo_learnings(
    owner: str,
    repo: str,
    platform: str = "github",
    *,
    on_progress: Any | None = None,
) -> dict[str, int]:
    """Run deterministic + LLM rule synthesis once for a repo's feedback store.

    Used by historical backfill after all PRs have been recorded, and by
    admin/CLI re-synthesize (no GitHub fetch). Always forces LLM when enough
    human_review events exist. Clears auto-synth Pending before re-run.
    """
    store = _open_store(owner, repo, platform)
    try:
        human_events = [e for e in store.list_feedback(limit=2000) if e.signal == "human_review"]
        deterministic_rules, llm_rules = await _synthesize_from_store(
            store,
            run_llm=len(human_events) >= 2,
            force_llm=True,
            on_progress=on_progress,
            replace_pending=True,
        )
    finally:
        store.close()
    return {"deterministic_rules": deterministic_rules, "llm_rules": llm_rules}
