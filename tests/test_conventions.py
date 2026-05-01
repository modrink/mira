"""Tests for team-conventions extraction."""

from __future__ import annotations

from mira.index.conventions import (
    _MAX_TOTAL_CHARS,
    extract_conventions,
    is_conventions_file,
)


class TestExtractConventions:
    def test_returns_empty_when_no_files(self):
        assert extract_conventions({}) == ""
        assert extract_conventions({"README.md": "hi"}) == ""

    def test_extracts_from_contributing(self):
        result = extract_conventions(
            {
                "CONTRIBUTING.md": "# Style\n\nUse `interface` not `type`.",
            }
        )
        assert "Use `interface` not `type`" in result
        assert "From `CONTRIBUTING.md`" in result

    def test_agents_md_takes_priority(self):
        # AGENTS.md is listed first in the priority order; both should
        # appear, but AGENTS.md first.
        result = extract_conventions(
            {
                "AGENTS.md": "AGENTS rule.",
                "CONTRIBUTING.md": "CONTRIBUTING rule.",
            }
        )
        assert result.index("AGENTS.md") < result.index("CONTRIBUTING.md")

    def test_strips_boilerplate_headers(self):
        # Sections like "Installation" / "License" don't carry coding rules.
        result = extract_conventions(
            {
                "CONTRIBUTING.md": (
                    "## Installation\n\n"
                    "Run `npm install`.\n\n"
                    "## Style\n\n"
                    "Use `interface` not `type`.\n\n"
                    "## License\n\n"
                    "MIT.\n"
                ),
            }
        )
        assert "Use `interface`" in result
        assert "npm install" not in result
        assert "MIT" not in result

    def test_caps_total_size(self):
        big = "x" * (_MAX_TOTAL_CHARS * 2)
        result = extract_conventions({"CONTRIBUTING.md": big})
        # Should be at or under the cap (allow for header overhead).
        assert len(result) <= _MAX_TOTAL_CHARS + 100

    def test_skips_empty_content(self):
        assert extract_conventions({"CONTRIBUTING.md": "", "AGENTS.md": "   \n"}) == ""

    def test_cursor_rules_supported(self):
        result = extract_conventions({".cursorrules": "Always use named exports."})
        assert "named exports" in result

    def test_copilot_instructions_supported(self):
        result = extract_conventions(
            {
                ".github/copilot-instructions.md": "Prefer functional style.",
            }
        )
        assert "functional style" in result


class TestIsConventionsFile:
    def test_recognizes_known_files(self):
        assert is_conventions_file("CONTRIBUTING.md")
        assert is_conventions_file("AGENTS.md")
        assert is_conventions_file(".cursorrules")
        assert is_conventions_file(".github/copilot-instructions.md")

    def test_rejects_unrelated(self):
        assert not is_conventions_file("README.md")
        assert not is_conventions_file("src/main.py")
        assert not is_conventions_file("LICENSE")
