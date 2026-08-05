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
_INJECT_BODY_MAX = int(os.environ.get("MIRA_LEARNED_INJECT_BODY_MAX", "600"))
_DETECTOR_RE = re.compile(r"(?im)^look for:\s*\S+")


def normalize_rule_text(rule_text: str) -> str:
    """Collapse whitespace + case for dedupe keys."""
    return " ".join(rule_text.lower().split())


_RATIONALE_PAREN_RE = re.compile(
    r"\s*\((?:[^)]*\b(?:team\s+values|team\s+prioritizes|team\s+emphasizes|"
    r"team\s+prefers|team\s+ensures|maintainability|modularity|localization)\b[^)]*)\)\s*$",
    re.IGNORECASE,
)


def strip_synth_rationale(rule_text: str) -> str:
    """Drop trailing parenthetical fluff formerly glued on from LLM rationale."""
    text = (rule_text or "").strip()
    if not text:
        return ""
    cleaned = _RATIONALE_PAREN_RE.sub("", text).strip()
    return cleaned or text


# Soft slogans that are not actionable for a review LLM (stack-neutral).
_SLOGAN_RE = re.compile(
    r"(?i)(?:"
    r"\bwhen\s+(?:it\s+is\s+)?(?:unnecessary|appropriate|redundant)\b|"
    r"\bwhen\s+they\s+are\s+not\s+needed\b|"
    r"\bavoid\s+unnecessary\b|"
    r"\bunnecessary\s+quer(?:y|ies)\b|"
    r"\bto\s+(?:simplify|improve|ensure|reduce)\b|"
    r"\bfor\s+(?:consistency|maintainability|readability|performance|"
    r"clarity|atomicity|better\s+semantic)\b|"
    r"\bfor better\b|"
    r"\bconsider\b|"
    r"\bmaintainability\b|"
    r"\breusabilit(?:y|ies)\b|"
    r"\bseparation of concerns\b|"
    r"\bcode organization\b|"
    r"\bclean(?:er)? codebase\b|"
    r"\bextract reusable\b|"
    r"\bhandle\s+edge\s+cases\b|"
    r"\b(?:comments?\s+)?(?:must\s+)?add\s+value\b|"
    r"\bbest\s+practices?\b|"
    r"\bnaming conventions?\b|"
    r"\bsplit\s+(?:\w+\s+)+into\s+smaller\b|"
    r"\bensures?\s+conditions?\s+are\s+explicit\b"
    r")"
)

# Soft titles that almost always produce mush.
_SOFT_TITLE_RE = re.compile(
    r"(?i)^(?:consider|ensure|try to|remember to|make sure|it is recommended)\b"
)

# Detector names a vague theme or the preferred fix instead of a smell.
_WEAK_DETECTOR_RE = re.compile(
    r"(?im)^look for:\s*(?:"
    r"unused\b|"
    r"large\b|"
    r"non-standard\b|"
    r"repeated\b|"
    r"properties that\b|"
    r"inline custom\b|"
    r"multiple database updates\b|"
    r"nullable\b|"
    r"200\s+ok\b|"
    r"`?decimal`?\s*$|"
    r"`?cache-tags`?\s*$"
    r")"
)


def pack_learned_rule(title: str, body: str) -> str:
    """Pack title + body into ``rule_text`` (``title\\n\\nbody``)."""
    title = " ".join((title or "").split()).strip()
    body = (body or "").strip()
    if not title or not body:
        return ""
    return f"{title}\n\n{body}"


def unpack_learned_rule(rule_text: str) -> tuple[str, str]:
    """Split packed ``rule_text`` into ``(title, body)``.

    Legacy one-liner rules (no blank line) return ``("", full_text)`` so UI
    still shows the text and inject can treat the whole string as content.
    """
    text = strip_synth_rationale(rule_text)
    if not text:
        return "", ""
    if "\n\n" not in text:
        return "", text
    title, _, body = text.partition("\n\n")
    title = " ".join(title.split()).strip()
    body = body.strip()
    if not title or not body:
        return "", text
    return title, body


def has_detector_line(body: str) -> bool:
    """True when body contains a ``Look for:`` detector line."""
    return bool(_DETECTOR_RE.search(body or ""))


def has_weak_detector(body: str) -> bool:
    """True when Look for: line is too vague or names the fix, not the smell."""
    return bool(_WEAK_DETECTOR_RE.search(body or ""))


def is_slogan_rule(rule_text: str) -> bool:
    """True when text looks like an unscoped soft slogan (drop / rewrite)."""
    text = strip_synth_rationale(rule_text)
    if not text:
        return True
    if _SLOGAN_RE.search(text):
        return True
    lower = text.lower()
    return bool(
        re.match(r"^(?:always\s+)?(?:use|avoid|prefer)\b", lower)
        and not re.search(
            r"\b(?:when|where|instead of|rather than|outside|within|over|before|after|"
            r"unless|for files|in the |on the |look for)\b",
            lower,
        )
    )


def rule_text_from_synth_action(item: dict) -> str:
    """Build packed title+body from synth fields, else legacy ``rule`` string.

    Returns empty string when the action fails the titled-rule gate.
    """
    title = str(item.get("title") or "").strip()
    body = str(item.get("body") or "").strip()
    if title or body:
        packed = pack_learned_rule(title, body)
        if not packed or not _passes_titled_gate(packed):
            return ""
        return packed

    legacy = strip_synth_rationale(str(item.get("rule") or ""))
    if not legacy or is_slogan_rule(legacy):
        return ""
    # Legacy flat rule: accept only if it already looks titled+detector, or
    # has enough substance for inject (pre-overhaul rows).
    t, b = unpack_learned_rule(legacy)
    if t and b:
        return legacy if _passes_titled_gate(legacy) else ""
    return legacy if len(legacy) >= 20 and not is_slogan_rule(legacy) else ""


def _passes_titled_gate(packed: str) -> bool:
    title, body = unpack_learned_rule(packed)
    if not title or not body:
        return False
    if len(title) > _TITLE_MAX or len(body) > _BODY_MAX:
        return False
    if _SOFT_TITLE_RE.search(title) or _SLOGAN_RE.search(title):
        return False
    # Prefer/Use titles OK when body has a concrete detector; still drop soft bodies.
    if is_slogan_rule(body) or is_slogan_rule(packed):
        return False
    if not has_detector_line(body):
        return False
    return not has_weak_detector(body)


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
    """Cap inject body length while keeping the ``Look for:`` line."""
    text = (body or "").strip()
    cap = _INJECT_BODY_MAX if limit is None else limit
    if not text or len(text) <= cap:
        return text
    lines = text.splitlines()
    detector = ""
    rest: list[str] = []
    for line in lines:
        if _DETECTOR_RE.match(line.strip()):
            detector = line.strip()
        else:
            rest.append(line)
    head = "\n".join(rest).strip()
    room = max(40, cap - len(detector) - 4)
    if len(head) > room:
        head = head[:room].rstrip() + "…"
    if detector:
        return f"{head}\n\n{detector}".strip() if head else detector
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
        raw = strip_synth_rationale(str(getattr(row, "rule_text", "") or ""))
        if not raw:
            continue
        title, body = unpack_learned_rule(raw)
        if title and body:
            out.append({"title": title, "content": truncate_inject_body(body)})
        else:
            # Legacy one-liner: use a short title from the first clause.
            title = raw.split(".")[0].strip()[:_TITLE_MAX] or "Team preference"
            out.append({"title": title, "content": truncate_inject_body(raw)})
        if len(out) >= limit:
            break
    return out


def select_learned_rule_texts(
    rules: list[Any],
    file_paths: list[str] | None = None,
    *,
    limit: int = 10,
) -> list[str]:
    """Legacy flat strings for critics / store helpers that expect bullets."""
    return [
        f"{r['title']}: {r['content']}" if r.get("title") else r["content"]
        for r in select_learned_rules(rules, file_paths, limit=limit)
    ]


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
