"""Helpers for selecting, packing, and matching learned rules at review time."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from difflib import SequenceMatcher
from typing import Any

# Internal upsert identity keys (human_pattern content hashes), not file globs.
_INTERNAL_PATH_RE = re.compile(r"^__[\w]+__$")
_NEAR_DUPE_RATIO = 0.80
_TITLE_MAX = 80
_BODY_MAX = 2000
_SYNTH_BODY_MIN = 120
_INJECT_BODY_MAX = int(os.environ.get("MIRA_LEARNED_INJECT_BODY_MAX", "600"))
_ALNUM_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.I)
# Non-actionable hedges / question-shaped guidance (synth quality gate).
_MUSH_BODY_RE = re.compile(
    r"\b("
    r"if necessary|check if|consider (?:whether|using|if)|"
    r"in the context|for better (?:readability|user experience)|"
    r"improve readability|when possible|be mindful|"
    r"may not be efficient"
    r")\b",
    re.I,
)
_CUE_RE = re.compile(
    r"`[^`]+`"  # backtick span
    r"|[A-Z][A-Za-z0-9_]*::"  # Class::
    r"|[A-Za-z_][A-Za-z0-9_]*\("  # call(
)


def normalize_rule_text(rule_text: str) -> str:
    """Collapse whitespace + case for dedupe keys."""
    return " ".join(rule_text.lower().split())


def _alnum_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _ALNUM_TOKEN_RE.finditer(text or "")}


def body_has_mush(text: str) -> bool:
    """True when body/title uses non-actionable hedge phrasing."""
    return bool(_MUSH_BODY_RE.search(text or ""))


def body_passes_synth_gate(title: str, body: str) -> bool:
    """Medium inject-fitted body: length, no mush, not a title paraphrase.

    Soft org conventions without backticks still pass when long enough and
    not a near restatement of the title.
    """
    title = " ".join((title or "").split()).strip()
    body = (body or "").strip()
    if not title or not body:
        return False
    if len(body) < _SYNTH_BODY_MIN:
        return False
    if body_has_mush(title) or body_has_mush(body):
        return False
    title_toks = _alnum_tokens(title)
    body_toks = _alnum_tokens(body)
    if title_toks:
        overlap = len(title_toks & body_toks) / len(title_toks)
        has_cue = bool(_CUE_RE.search(body))
        novel = body_toks - title_toks
        # Title paraphrase with no extra signal → fail.
        if overlap >= 0.7 and not has_cue and len(novel) < 4:
            return False
    return True


def pack_learned_rule(title: str, body: str) -> str:
    """Pack title + body into ``rule_text`` (``title\\n\\nbody``)."""
    title = " ".join((title or "").split()).strip()
    body = (body or "").strip()
    if not title or not body:
        return ""
    if len(title) > _TITLE_MAX or len(body) > _BODY_MAX:
        return ""
    return f"{title}\n\n{body}"


def unpack_learned_rule(rule_text: str) -> tuple[str, str]:
    """Split packed ``rule_text`` into ``(title, body)``. Empty if not titled."""
    text = (rule_text or "").strip()
    if not text or "\n\n" not in text:
        return "", ""
    title, _, body = text.partition("\n\n")
    title = " ".join(title.split()).strip()
    body = body.strip()
    if not title or not body:
        return "", ""
    return title, body


def rule_text_from_synth_action(item: dict) -> str:
    """Pack title+body from a synth action. Empty when size/title gate fails.

    Does not apply the Medium substance gate — callers that mint Pending from
    LLM extract should use :func:`body_passes_synth_gate` separately.
    """
    return pack_learned_rule(str(item.get("title") or ""), str(item.get("body") or ""))


# Synth path scopes: widen deep folders to at most this many segments.
_MAX_PATH_HINT_DEPTH = 2


def sanitize_path_hint(path_hint: str) -> str:
    """Normalize to a shallow ``dir/**`` glob, or "" when not useful.

    LLM chooses scope; code only caps depth (e.g. ``a/b/c/d/**`` → ``a/b/**``)
    so comment-leaf folders do not over-restrict inject. Stack-agnostic — no
    framework allowlist.
    """
    hint = " ".join((path_hint or "").split()).strip().strip("`")
    if not hint:
        return ""
    # Strip accidental identity suffix if a full path_pattern was passed.
    scope, _ident = split_path_pattern(hint)
    if scope:
        hint = scope
    hint = hint.replace("\\", "/").strip("/")
    if not hint:
        return ""
    # Single bare filename → not a useful scope.
    if "/" not in hint and not any(ch in hint for ch in "*?[") and "." in hint:
        return ""
    # Drop glob noise; keep directory segments only.
    parts: list[str] = []
    for part in hint.replace("/**", "/").rstrip("/").split("/"):
        if not part or part in ("*", "**"):
            continue
        if any(ch in part for ch in "*?["):
            continue
        parts.append(part)
    if not parts:
        return ""
    # File path → drop filename, keep parent dirs.
    if "." in parts[-1]:
        parts = parts[:-1]
    if not parts:
        return ""
    if len(parts) > _MAX_PATH_HINT_DEPTH:
        parts = parts[:_MAX_PATH_HINT_DEPTH]
    return "/".join(parts) + "/**"


def human_pattern_key(rule_text: str) -> str:
    """Stable upsert path for a human-pattern rule (content identity)."""
    digest = hashlib.sha256(normalize_rule_text(rule_text).encode()).hexdigest()[:16]
    return f"__human_{digest}__"


def human_pattern_path(rule_text: str, path_hint: str = "") -> str:
    """Upsert path_pattern: optional file scope + content identity."""
    identity = human_pattern_key(rule_text)
    scope = sanitize_path_hint(path_hint)
    if scope:
        return f"{scope}::{identity}"
    return identity


def split_path_pattern(path_pattern: str) -> tuple[str, str]:
    """Return ``(file_scope, identity_or_full)`` for stored path_pattern."""
    pat = (path_pattern or "").strip()
    if "::" in pat:
        scope, _, rest = pat.partition("::")
        if scope and _INTERNAL_PATH_RE.match(rest):
            return scope, rest
    return "", pat


def is_file_scope_pattern(path_pattern: str) -> bool:
    """True when ``path_pattern`` carries a real file glob (not only identity)."""
    scope, ident = split_path_pattern(path_pattern)
    if scope:
        return True
    pat = (ident or path_pattern or "").strip()
    return bool(pat) and not _INTERNAL_PATH_RE.match(pat)


def display_path_pattern(path_pattern: str) -> str:
    """Human-visible path chip (scope only; hide identity keys)."""
    scope, ident = split_path_pattern(path_pattern)
    if scope:
        return scope
    if _INTERNAL_PATH_RE.match(ident or ""):
        return ""
    return ident or ""


def path_matches_rule_pattern(file_path: str, path_pattern: str) -> bool:
    """Match a repo-relative file path against a learned-rule path pattern."""
    scope, ident = split_path_pattern(path_pattern)
    pat = scope or (path_pattern or "").strip()
    if not pat or _INTERNAL_PATH_RE.match(pat) or _INTERNAL_PATH_RE.match(ident or ""):
        if scope:
            pat = scope
        else:
            return True
    path = file_path.lstrip("./")
    if pat.endswith("/**"):
        prefix = pat[:-3].rstrip("/")
        if not prefix:
            return True
        return path == prefix or path.startswith(prefix + "/")
    if pat.endswith("/"):
        prefix = pat.rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    filename = path.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(filename, pat)


def rule_applies_to_files(path_pattern: str, file_paths: list[str]) -> bool:
    """True if the rule should inject for this chunk/PR file set."""
    if not is_file_scope_pattern(path_pattern):
        return True
    if not file_paths:
        return True
    return any(path_matches_rule_pattern(p, path_pattern) for p in file_paths)


def truncate_inject_body(body: str, *, limit: int | None = None) -> str:
    """Cap inject body length."""
    text = (body or "").strip()
    cap = _INJECT_BODY_MAX if limit is None else limit
    if not text or len(text) <= cap:
        return text
    return text[:cap].rstrip() + "…"


def select_learned_rules(
    rules: list[Any],
    file_paths: list[str] | None = None,
    *,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Pick approved learned rules as ``{title, content}`` for review inject."""
    paths = list(file_paths) if file_paths is not None else None
    out: list[dict[str, str]] = []
    for row in rules:
        if getattr(row, "source_signal", "") == "accept_pattern":
            continue
        if paths is not None and not rule_applies_to_files(
            str(getattr(row, "path_pattern", "") or ""), paths
        ):
            continue
        raw = str(getattr(row, "rule_text", "") or "").strip()
        title, body = unpack_learned_rule(raw)
        if not title or not body:
            continue
        out.append({"title": title, "content": truncate_inject_body(body)})
        if len(out) >= limit:
            break
    return out


def select_learned_rule_texts(
    rules: list[Any],
    file_paths: list[str] | None = None,
    *,
    limit: int = 10,
) -> list[str]:
    """Flat ``title: body`` strings for critics / helpers that want bullets."""
    return [f"{r['title']}: {r['content']}" for r in select_learned_rules(rules, file_paths, limit=limit)]


def find_near_duplicate_rule(
    rule_text: str,
    catalog: list[Any],
    *,
    ratio: float = _NEAR_DUPE_RATIO,
) -> Any | None:
    """Return catalog row whose text is near-duplicate of ``rule_text``, else None."""
    target = normalize_rule_text(rule_text)
    if not target:
        return None
    best = None
    best_ratio = 0.0
    for row in catalog:
        other = normalize_rule_text(str(getattr(row, "rule_text", "") or ""))
        if not other:
            continue
        if other == target:
            return row
        r = SequenceMatcher(None, target, other).ratio()
        if r > best_ratio:
            best_ratio = r
            best = row
    if best is not None and best_ratio >= ratio:
        return best
    return None
