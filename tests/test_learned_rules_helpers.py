"""Tests for path-scoped learned rules, pack/unpack, and titled synth gates."""

from __future__ import annotations

from types import SimpleNamespace

from mira.analysis.learned_rules import (
    display_path_pattern,
    find_near_duplicate_rule,
    has_detector_line,
    human_pattern_path,
    is_slogan_rule,
    pack_learned_rule,
    path_matches_rule_pattern,
    rule_applies_to_files,
    rule_text_from_synth_action,
    sanitize_path_hint,
    select_learned_rule_texts,
    select_learned_rules,
    strip_synth_rationale,
    unpack_learned_rule,
)


def test_internal_hash_keys_match_everywhere():
    assert path_matches_rule_pattern("src/a.py", "__human_abc123__")
    assert rule_applies_to_files("__human_abc123__", ["tests/x.py"])


def test_strip_synth_rationale_drops_team_fluff():
    raw = (
        "Extract large Alpine.js x-data logic into separate files or components. "
        "(The team values modularity and maintainability in frontend logic.)"
    )
    assert strip_synth_rationale(raw) == (
        "Extract large Alpine.js x-data logic into separate files or components."
    )
    assert strip_synth_rationale("Keep #[Locked] attributes.") == "Keep #[Locked] attributes."


def test_pack_unpack_round_trip():
    packed = pack_learned_rule(
        "Prefer braced control structures",
        "Always brace if/for/while bodies.\n\nLook for: braceless if/for/while",
    )
    title, body = unpack_learned_rule(packed)
    assert title == "Prefer braced control structures"
    assert "Look for: braceless if/for/while" in body
    assert unpack_learned_rule("legacy one-liner without blank line") == (
        "",
        "legacy one-liner without blank line",
    )
    assert pack_learned_rule("", "body") == ""
    assert pack_learned_rule("title", "") == ""


def test_titled_gate_requires_detector():
    assert has_detector_line("Context here.\n\nLook for: bare except")
    assert not has_detector_line("Context with no detector")
    good = rule_text_from_synth_action(
        {
            "title": "Catch specific exceptions",
            "body": (
                "Prefer named exception types over bare except.\n\nLook for: bare except clauses"
            ),
        }
    )
    assert good.startswith("Catch specific exceptions\n\n")
    assert "Look for: bare except clauses" in good
    assert (
        rule_text_from_synth_action(
            {
                "title": "Avoid unnecessary queries",
                "body": "Skip queries when not needed.\n\nLook for: extra queries",
            }
        )
        == ""
    )
    assert (
        rule_text_from_synth_action(
            {
                "title": "Use the repository layer",
                "body": "Handlers should not call the DB directly.",
            }
        )
        == ""
    )
    assert rule_text_from_synth_action({"rule": "Use X for consistency."}) == ""


def test_select_learned_rules_returns_title_content():
    rows = [
        SimpleNamespace(
            rule_text=pack_learned_rule(
                "Flag raw SQL",
                "Keep SQL in the data layer.\n\nLook for: ad-hoc SQL in handlers",
            ),
            path_pattern="",
            source_signal="human_pattern",
        ),
    ]
    out = select_learned_rules(rows, ["a.py"])
    assert len(out) == 1
    assert out[0]["title"] == "Flag raw SQL"
    assert "Look for: ad-hoc SQL" in out[0]["content"]
    # Legacy helper still returns flat strings for critics.
    flat = select_learned_rule_texts(rows, ["a.py"])
    assert flat[0].startswith("Flag raw SQL:")


def test_dir_glob_prefix():
    assert path_matches_rule_pattern("src/auth/login.py", "src/**")
    assert path_matches_rule_pattern("src/auth.py", "src/**")
    assert not path_matches_rule_pattern("tests/auth.py", "src/**")
    assert rule_applies_to_files("src/**", ["tests/a.py", "src/b.py"])
    assert not rule_applies_to_files("src/**", ["tests/a.py"])


def test_scoped_identity_path_pattern():
    packed = pack_learned_rule(
        "Index analytics tables",
        "Add indexes on filter columns.\n\nLook for: Schema::create without indexes",
    )
    pat = human_pattern_path(packed, "migrations/**")
    assert pat.startswith("migrations/**::__human_")
    assert display_path_pattern(pat) == "migrations/**"
    assert path_matches_rule_pattern("migrations/2024_01_01.sql", pat)
    assert not path_matches_rule_pattern("src/models/user.py", pat)


def test_select_filters_by_path():
    rows = [
        SimpleNamespace(
            rule_text="global tip",
            path_pattern="",
            source_signal="human_pattern",
        ),
        SimpleNamespace(
            rule_text="src only",
            path_pattern="src/**",
            source_signal="reject_pattern",
        ),
        SimpleNamespace(
            rule_text="noise",
            path_pattern="",
            source_signal="accept_pattern",
        ),
    ]
    assert select_learned_rule_texts(rows, ["tests/a.py"]) == ["global tip: global tip"]
    # legacy one-liner gets synthetic title from first clause
    assert "src only" in select_learned_rule_texts(rows, ["src/a.py"])[1]


def test_near_dupe_detects_similar_wording():
    catalog = [
        SimpleNamespace(
            id=1,
            rule_text="Prefer Alpine for lightweight Docker images.",
        ),
        SimpleNamespace(
            id=2,
            rule_text="Always validate auth tokens at the edge.",
        ),
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
    # Depth ≤2 widen; LLM chooses scope — no framework allowlist.
    assert sanitize_path_hint("src/models/**") == "src/models/**"
    assert sanitize_path_hint("migrations/**") == "migrations/**"
    assert sanitize_path_hint("migrations/2024_01_01_foo.sql") == "migrations/**"
    assert sanitize_path_hint("src/widgets/panel/Modal.tsx") == "src/widgets/**"
    assert sanitize_path_hint("app/services/billing/invoices/**") == "app/services/**"
    assert sanitize_path_hint("app/http/controllers/api/v1/**") == "app/http/**"
    assert sanitize_path_hint("Panel.php") == ""
    assert sanitize_path_hint("") == ""


def test_truncate_inject_body_keeps_detector():
    from mira.analysis.learned_rules import truncate_inject_body

    body = "Context " + ("x" * 800) + "\n\nLook for: bare except"
    out = truncate_inject_body(body, limit=120)
    assert "Look for: bare except" in out
    assert len(out) <= 130


def test_slogan_gate():
    assert is_slogan_rule("Avoid using query() when it is unnecessary.")
    assert is_slogan_rule("Use transactions for database operations to ensure consistency.")
    assert is_slogan_rule("Handle edge cases for nullable variables.")
    assert is_slogan_rule("Use camelCase for variable names.")
    assert is_slogan_rule("Extract reusable methods into traits for better reusability.")
    assert not is_slogan_rule("Flag raw SQL queries outside the data layer.")
    assert not is_slogan_rule("Prefer braced bodies.\n\nLook for: braceless if statements")


def test_weak_detector_and_soft_title_gate():
    assert (
        rule_text_from_synth_action(
            {
                "title": "Consider using withoutOverlapping",
                "body": ("Avoid overlapping scheduled tasks.\n\nLook for: withoutOverlapping()"),
            }
        )
        == ""
    )
    assert (
        rule_text_from_synth_action(
            {
                "title": "Use decimal instead of float",
                "body": ("Prefer decimal for money columns.\n\nLook for: decimal"),
            }
        )
        == ""
    )
    assert (
        rule_text_from_synth_action(
            {
                "title": "Remove unused enum values",
                "body": ("Drop unused enum cases.\n\nLook for: Unused enum values."),
            }
        )
        == ""
    )
    good = rule_text_from_synth_action(
        {
            "title": "Prefer decimal for money columns",
            "body": (
                "Use decimal instead of float for monetary amounts.\n\n"
                "Look for: float columns in money/amount migrations"
            ),
        }
    )
    assert "Prefer decimal for money columns" in good
    assert "Look for: float columns" in good
