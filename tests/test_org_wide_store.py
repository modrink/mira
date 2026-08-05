"""Org-wide SQLite aggregation must cover GitLab (namespaced) repos too."""

from __future__ import annotations

import pytest

from mira.index.store import (
    IndexStore,
    _iter_repo_dbs,
    list_learned_rules_org_wide_sqlite,
    search_packages_org_wide_sqlite,
)

_PKG = [{"name": "lodash", "kind": "npm", "version": "4.17.20", "file_path": "package.json"}]


@pytest.fixture
def index_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    return tmp_path


def test_iter_finds_github_and_gitlab(index_dir):
    IndexStore.open("acme", "web").close()
    IndexStore.open("group/sub", "web", platform="gitlab").close()
    found = {(p, o, r) for p, o, r, _ in _iter_repo_dbs(str(index_dir))}
    assert ("github", "acme", "web") in found
    assert ("gitlab", "group/sub", "web") in found  # nested-group owner preserved


def test_package_search_includes_gitlab(index_dir):
    gh = IndexStore.open("acme", "web")
    gh.replace_manifest_packages("package.json", _PKG)
    gh.close()
    gl = IndexStore.open("grp", "app", platform="gitlab")
    gl.replace_manifest_packages("package.json", _PKG)
    gl.close()

    hits = search_packages_org_wide_sqlite(name="lodash")
    by_platform = {(h["platform"], h["owner"], h["repo"]) for h in hits}
    assert ("github", "acme", "web") in by_platform
    assert ("gitlab", "grp", "app") in by_platform


def test_org_learned_rules_hide_accept_pattern(index_dir):
    store = IndexStore.open("acme", "web")
    store.upsert_learned_rule(
        rule_text="noise accept",
        source_signal="accept_pattern",
        category="style",
        path_pattern="",
        sample_count=5,
    )
    store.upsert_learned_rule(
        rule_text="real reject",
        source_signal="reject_pattern",
        category="style",
        path_pattern="",
        sample_count=5,
    )
    store.close()

    rows = list_learned_rules_org_wide_sqlite()
    assert all(r["source_signal"] != "accept_pattern" for r in rows)
    assert any(r["rule_text"] == "real reject" for r in rows)
    assert any(r.get("platform") == "github" for r in rows)


def test_decode_store_owner_key_roundtrip():
    from mira.index.store import decode_store_owner_key, encode_store_owner_key

    assert encode_store_owner_key("acme", "github") == "acme"
    assert encode_store_owner_key("acme", "gitlab") == "_gitlab/acme"
    assert decode_store_owner_key("_gitlab/group/sub") == ("gitlab", "group/sub")
    assert decode_store_owner_key("acme") == ("github", "acme")
    key = encode_store_owner_key("grp/sub", "forgejo")
    assert decode_store_owner_key(key) == ("forgejo", "grp/sub")
