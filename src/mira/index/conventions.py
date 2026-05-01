"""Extract team coding conventions from a repo's contributor-facing docs.

The output is injected into the review prompt so Mira flags violations that
are *specific to this team* (e.g. "prefer interface over type", "no ternaries
in this codebase") rather than only generic best-practices.

Files we look at, in priority order:

  - ``AGENTS.md`` — explicitly written for AI agents; takes precedence
  - ``CONTRIBUTING.md`` / ``CONVENTIONS.md`` / ``STYLE.md`` / ``STYLEGUIDE.md``
  - ``.github/copilot-instructions.md`` — used by GitHub Copilot for the same purpose
  - ``.cursorrules`` — Cursor's variant of the same idea

We deliberately skip ``.editorconfig`` (whitespace-only) and language-specific
config files like ``pyproject.toml [tool.ruff]`` — those drive the linter, which
catches their violations more reliably than an LLM would.

Total length is capped so the conventions block doesn't dominate the review
prompt's token budget.
"""

from __future__ import annotations

import re

# Ordered: earlier entries take priority when the cap is reached.
_CONVENTION_FILES = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "CONVENTIONS.md",
    "STYLE.md",
    "STYLEGUIDE.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
)

# Token budget (characters, not tokens — chars are a fine proxy at ~4:1 chars
# per token; this caps the conventions section at ~2K tokens of prompt budget).
_MAX_TOTAL_CHARS = 8_000
_MAX_PER_FILE_CHARS = 4_000


_BOILERPLATE_HEADERS = re.compile(
    r"^#{1,3}\s+(table of contents|toc|license|prerequisites|installation|"
    r"getting started|setup|how to run|running|build|deploy|"
    r"code of conduct|contributors|acknowledg(e?)ments|how to contribute|"
    r"reporting (issues|bugs)|filing (issues|bugs)|pull requests?|"
    r"opening (a )?pull request|getting help)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_boilerplate(text: str) -> str:
    """Trim setup/contribution-process headers that don't carry coding rules.

    A naive heuristic: when we see a known boilerplate H1/H2/H3, drop until
    the next header of the same or higher level. The rules-of-the-road
    sections (style, naming, testing) are what survive.
    """
    if not text:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    skip_until_level: int | None = None
    for line in lines:
        # Track header level
        m = re.match(r"^(#{1,6})\s+", line)
        level = len(m.group(1)) if m else None

        if skip_until_level is not None:
            if level is not None and level <= skip_until_level:
                skip_until_level = None
                # fall through and reconsider this header
            else:
                continue

        if level is not None and _BOILERPLATE_HEADERS.match(line):
            skip_until_level = level
            continue

        out.append(line)
    return "\n".join(out)


def extract_conventions(file_contents: dict[str, str]) -> str:
    """Build the conventions string from a ``{path: content}`` mapping.

    Returns the empty string if no convention files are found.
    """
    parts: list[str] = []
    used = 0
    for fname in _CONVENTION_FILES:
        content = file_contents.get(fname)
        if not content:
            continue
        body = _strip_boilerplate(content).strip()
        if not body:
            continue
        if len(body) > _MAX_PER_FILE_CHARS:
            body = body[:_MAX_PER_FILE_CHARS] + "\n…(truncated)"
        section = f"### From `{fname}`\n\n{body}"
        if used + len(section) > _MAX_TOTAL_CHARS:
            break
        parts.append(section)
        used += len(section)
    return "\n\n".join(parts)


def is_conventions_file(path: str) -> bool:
    """``True`` if ``path`` is one of the convention files we extract from.

    Used by the push handler to decide whether to re-extract on a change.
    """
    return path in _CONVENTION_FILES
