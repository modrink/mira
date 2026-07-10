"""PR-scoped incremental diff — clamp push deltas to the PR/MR file set."""

from __future__ import annotations

import re

from mira.core.diff_parser import parse_diff

_DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)


def pr_file_paths(pr_diff: str) -> set[str]:
    """Paths present in a platform PR/MR unified diff."""
    return {f.path for f in parse_diff(pr_diff).files}


def intersect_push_diff_with_pr(pr_diff: str, push_diff: str) -> str:
    """Keep only push-delta files that are also in the PR diff.

    Round 2+ reviews fetch ``last_reviewed_sha..head`` via the compare API,
    which can include upstream files after a merge-from-base. Intersecting
    with the PR file set matches GitHub/GitLab "Files changed" scope.
    """
    allowed = pr_file_paths(pr_diff)
    if not allowed or not push_diff.strip():
        return ""

    chunks: list[str] = []
    for part in re.split(r"(?=^diff --git )", push_diff, flags=re.MULTILINE):
        part = part.strip("\n")
        if not part:
            continue
        m = _DIFF_FILE_RE.search(part)
        if not m:
            continue
        b_path = m.group(2)
        a_path = m.group(1)
        if b_path in allowed or (b_path == "dev/null" and a_path in allowed):
            chunks.append(part)
    if not chunks:
        return ""
    return "\n".join(chunks) + "\n"
