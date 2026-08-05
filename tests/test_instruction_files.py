"""Repo instruction-file ingest into review custom_rules."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mira.analysis.instruction_files import load_instruction_custom_rules


@pytest.mark.asyncio
async def test_prefers_base_branch_over_base_ref():
    provider = AsyncMock()
    provider.get_file_content = AsyncMock(return_value="# Agents\nPrefer typed APIs.")
    pr = SimpleNamespace(base_branch="main", base_ref="wrong-ref", base_sha="")

    rules = await load_instruction_custom_rules(provider, pr)

    assert rules
    assert all(r["title"].startswith("Repo instructions (") for r in rules)
    assert "Prefer typed APIs" in rules[0]["content"]
    # Every fetch must use base_branch, not base_ref.
    for call in provider.get_file_content.await_args_list:
        assert call.args[2] == "main"


@pytest.mark.asyncio
async def test_falls_back_to_base_ref_when_branch_missing():
    provider = AsyncMock()
    provider.get_file_content = AsyncMock(return_value="style guide")
    pr = SimpleNamespace(base_ref="develop")

    await load_instruction_custom_rules(provider, pr)

    assert provider.get_file_content.await_args.args[2] == "develop"


@pytest.mark.asyncio
async def test_skips_when_no_ref():
    provider = AsyncMock()
    provider.get_file_content = AsyncMock()
    pr = SimpleNamespace()

    assert await load_instruction_custom_rules(provider, pr) == []
    provider.get_file_content.assert_not_called()
