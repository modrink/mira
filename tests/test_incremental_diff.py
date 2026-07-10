"""Tests for PR-scoped incremental diff helpers."""

from __future__ import annotations

import pytest

from mira.core.incremental_diff import intersect_push_diff_with_pr

_PR_POSTGRES = """diff --git a/src/mira/db/postgres.py b/src/mira/db/postgres.py
index 1111111..2222222 100644
--- a/src/mira/db/postgres.py
+++ b/src/mira/db/postgres.py
@@ -1 +1 @@
-old
+new
"""

_UPSTREAM_REPOS = """diff --git a/src/mira/dashboard/routers/repos.py b/src/mira/dashboard/routers/repos.py
index 3333333..4444444 100644
--- a/src/mira/dashboard/routers/repos.py
+++ b/src/mira/dashboard/routers/repos.py
@@ -1 +1 @@
-upstream
+upstream-main
"""

_ENGINE = """diff --git a/src/mira/core/engine.py b/src/mira/core/engine.py
index 1111111..2222222 100644
--- a/src/mira/core/engine.py
+++ b/src/mira/core/engine.py
@@ -1 +1 @@
-old
+new
"""


def test_intersect_keeps_pr_files_drops_upstream_merge():
    push = _PR_POSTGRES + "\n" + _UPSTREAM_REPOS
    scoped = intersect_push_diff_with_pr(_PR_POSTGRES, push)
    assert "src/mira/db/postgres.py" in scoped
    assert "repos.py" not in scoped


def test_intersect_empty_when_push_only_upstream():
    assert intersect_push_diff_with_pr(_PR_POSTGRES, _UPSTREAM_REPOS) == ""


@pytest.mark.parametrize(
    ("pr_diff", "push_diff"),
    [
        ("", _PR_POSTGRES),
        (_PR_POSTGRES, ""),
        (_PR_POSTGRES, "   \n\n  "),
    ],
)
def test_intersect_empty_inputs(pr_diff, push_diff):
    assert intersect_push_diff_with_pr(pr_diff, push_diff) == ""


def test_intersect_partial_pr_files_in_push():
    """Push only touches one of two PR files — keep just that chunk."""
    pr = _PR_POSTGRES + "\n" + _ENGINE
    scoped = intersect_push_diff_with_pr(pr, _PR_POSTGRES)
    assert "postgres.py" in scoped
    assert "engine.py" not in scoped


def test_intersect_handles_renamed_path_in_header():
    pr = """diff --git a/old/name.py b/src/new/name.py
index 1111111..2222222 100644
--- a/old/name.py
+++ b/src/new/name.py
@@ -1 +1 @@
-a
+b
"""
    push = """diff --git a/old/name.py b/src/new/name.py
index 1111111..3333333 100644
--- a/old/name.py
+++ b/src/new/name.py
@@ -1 +1 @@
-a
+c
"""
    scoped = intersect_push_diff_with_pr(pr, push)
    assert "src/new/name.py" in scoped


def test_intersect_new_file_uses_b_path():
    pr = """diff --git a/dev/null b/src/new/module.py
new file mode 100644
--- /dev/null
+++ b/src/new/module.py
@@ -0,0 +1 @@
+hello
"""
    push = """diff --git a/dev/null b/src/new/module.py
new file mode 100644
--- /dev/null
+++ b/src/new/module.py
@@ -0,0 +1 @@
+world
"""
    scoped = intersect_push_diff_with_pr(pr, push)
    assert "src/new/module.py" in scoped
    assert "world" in scoped


def test_intersect_deleted_file_matches_a_path_when_b_is_dev_null():
    """GitHub deletion headers use b/dev/null; PR paths come from the old path."""
    pr = """diff --git a/src/removed.py b/dev/null
deleted file mode 100644
--- a/src/removed.py
+++ /dev/null
@@ -1 +0,0 @@
-x
"""
    push = """diff --git a/src/removed.py b/dev/null
deleted file mode 100644
--- a/src/removed.py
+++ /dev/null
@@ -1 +0,0 @@
-y
"""
    scoped = intersect_push_diff_with_pr(pr, push)
    assert "src/removed.py" in scoped
