"""Load repo instruction files (AGENTS.md, CLAUDE.md, …) into review context."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Checked in order; first hit per basename wins. Nested paths last.
_INSTRUCTION_CANDIDATES = (
    "REVIEW.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
    ".gemini/styleguide.md",
)

_MAX_CHARS_PER_FILE = 4000
_MAX_FILES = 3


async def load_instruction_custom_rules(provider: Any, pr_info: Any) -> list[dict[str, str]]:
    """Fetch known instruction files from the PR base tip; return custom_rules dicts."""
    ref = (
        getattr(pr_info, "base_branch", None)
        or getattr(pr_info, "base_ref", None)
        or getattr(pr_info, "base_sha", None)
        or ""
    )
    if not ref or not hasattr(provider, "get_file_content"):
        return []

    out: list[dict[str, str]] = []
    for path in _INSTRUCTION_CANDIDATES:
        if len(out) >= _MAX_FILES:
            break
        try:
            raw = await provider.get_file_content(pr_info, path, ref)
        except Exception:
            continue
        text = (raw or "").strip()
        if not text:
            continue
        if len(text) > _MAX_CHARS_PER_FILE:
            text = text[:_MAX_CHARS_PER_FILE].rstrip() + "\n…"
        title = f"Repo instructions ({path})"
        out.append({"title": title, "content": text})
        logger.info("Ingested instruction file %s for review context", path)
    return out
