"""Tests for path-scoped learned rules, pack/unpack, and inject."""

from __future__ import annotations

from types import SimpleNamespace

from mira.analysis.learned_rules import (
    display_path_pattern,
    find_near_duplicate_rule,
    human_pattern_path,
    pack_learned_rule,
    path_matches_rule_pattern,
    rule_applies_to_files,
    rule_text_from_synth_action,
    sanitize_path_hint,
    select_learned_rule_texts,
    select_learned_rules,
    truncate_inject_body,
    unpack_learned_rule,
)


def test_internal_hash_keys_match_everywhere():
    assert path_matches_rule_pattern("src/a.py", "__human_abc123__")
    assert rule_applies_to_files("__human_abc123__", ["tests/x.py"])


def test_pack_unpack_roundtrip():
    packed = pack_learned_rule(
        "Catch specific exceptions",
        "Prefer named types over bare except.",
    )
    assert unpack_learned_rule(packed) == (
        "Catch specific exceptions",
        "Prefer named types over bare except.",
    )
    assert unpack_learned_rule("flat one-liner") == ("", "")
    assert pack_learned_rule("", "body") == ""
    assert pack_learned_rule("title", "") == ""


def test_gate_title_body_only():
    good = rule_text_from_synth_action(
        {
            "title": "Catch specific exceptions",
            "body": "Prefer named exception types over bare except.",
        }
    )
    assert good.startswith("Catch specific exceptions\n\n")
    title, body = unpack_learned_rule(good)
    assert body == "Prefer named exception types over bare except."
    soft = rule_text_from_synth_action(
        {
            "title": "Avoid unnecessary queries",
            "body": "Skip queries when not needed.",
        }
    )
    assert "Avoid unnecessary queries" in soft
    assert (
        rule_text_from_synth_action(
            {
                "title": "Use the repository layer",
                "body": "",
            }
        )
        == ""
    )
    assert rule_text_from_synth_action({"rule": "flat legacy string"}) == ""


def test_select_learned_rules_injects_body():
    rows = [
        SimpleNamespace(
            rule_text=pack_learned_rule(
                "Flag raw SQL",
                "Keep SQL in the data layer. Prefer ad-hoc SQL only in handlers.",
            ),
            path_pattern="",
            source_signal="human_pattern",
        ),
    ]
    out = select_learned_rules(rows, ["a.py"])
    assert out[0]["title"] == "Flag raw SQL"
    assert out[0]["content"] == (
        "Keep SQL in the data layer. Prefer ad-hoc SQL only in handlers."
    )


def test_dir_glob_prefix():
    assert path_matches_rule_pattern("src/auth/login.py", "src/**")
    assert path_matches_rule_pattern("src/auth.py", "src/**")
    assert not path_matches_rule_pattern("tests/auth.py", "src/**")
    assert rule_applies_to_files("src/**", ["tests/a.py", "src/b.py"])
    assert not rule_applies_to_files("src/**", ["tests/a.py"])


def test_scoped_identity_path_pattern():
    packed = pack_learned_rule(
        "Index analytics tables",
        "Add indexes on filter columns.",
    )
    pat = human_pattern_path(packed, "migrations/**")
    assert pat.startswith("migrations/**::__human_")
    assert display_path_pattern(pat) == "migrations/**"
    assert path_matches_rule_pattern("migrations/2024_01_01.sql", pat)
    assert not path_matches_rule_pattern("src/models/user.py", pat)


def test_select_filters_by_path():
    rows = [
        SimpleNamespace(
            rule_text=pack_learned_rule("Global tip", "Applies everywhere."),
            path_pattern="",
            source_signal="human_pattern",
        ),
        SimpleNamespace(
            rule_text=pack_learned_rule("Src only", "Only under src."),
            path_pattern="src/**",
            source_signal="reject_pattern",
        ),
        SimpleNamespace(
            rule_text=pack_learned_rule("Noise", "Should be skipped."),
            path_pattern="",
            source_signal="accept_pattern",
        ),
    ]
    texts = select_learned_rule_texts(rows, ["tests/a.py"])
    assert len(texts) == 1
    assert texts[0].startswith("Global tip:")
    texts_src = select_learned_rule_texts(rows, ["src/a.py"])
    assert len(texts_src) == 2


def test_near_dupe_detects_similar_wording():
    catalog = [
        SimpleNamespace(id=1, rule_text="Prefer Alpine for lightweight Docker images."),
        SimpleNamespace(id=2, rule_text="Always validate auth tokens at the edge."),
    ]
    near = find_near_duplicate_rule(
        "Prefer Alpine for lightweight Docker images in production.",
        catalog,
    )
    assert near is not None
    assert near.id == 1
    assert find_near_duplicate_rule("Completely unrelated security rule.", catalog) is None


def test_pack_unpack_human_comment_with_hunk():
    from mira.analysis.feedback import (
        pack_human_comment_for_learning,
        unpack_human_comment_for_synth,
    )

    packed = pack_human_comment_for_learning(
        "Prefer Cache::remember here",
        "@@ -1,3 +1,4 @@\n-Cache::get\n+Cache::remember",
    )
    body, code = unpack_human_comment_for_synth(packed)
    assert "Cache::remember" in body
    assert "Cache::remember" in code
    assert unpack_human_comment_for_synth("plain comment") == ("plain comment", "")


def test_sanitize_path_hint():
    assert sanitize_path_hint("src/models/**") == "src/models/**"
    assert sanitize_path_hint("migrations/**") == "migrations/**"
    assert sanitize_path_hint("migrations/2024_01_01_foo.sql") == "migrations/**"
    assert sanitize_path_hint("src/widgets/panel/Modal.tsx") == "src/widgets/**"
    assert sanitize_path_hint("app/services/billing/invoices/**") == "app/services/**"
    assert sanitize_path_hint("app/http/controllers/api/v1/**") == "app/http/**"
    assert sanitize_path_hint("Panel.php") == ""
    assert sanitize_path_hint("") == ""


def test_truncate_inject_body():
    body = "Context " + ("x" * 800)
    out = truncate_inject_body(body, limit=120)
    assert len(out) <= 121
    assert out.endswith("…")
