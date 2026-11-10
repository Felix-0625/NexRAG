"""Audited local tools and account-scoped file inspection."""

import asyncio
from decimal import Decimal

import pytest
from pydantic_ai.models.test import TestModel

from src.agent.agent import AgentDeps, AgenticRunner, UserContext
from src.agent.builtin_tools import DISCOUNT_TOOL_ID, calculate_discount_quote
from src.agent.tools import inspect_temporary_file
from src.config import settings
from src.midplatform.data_policy import save_account_policy, save_resource_policy


def test_decimal_quote_is_exact():
    result = calculate_discount_quote(2000, 6)
    assert result == {
        "original_amount": "2000.00",
        "discount_percent": "6",
        "discount_amount": "120.00",
        "quote_amount": "1880.00",
    }
    assert calculate_discount_quote(Decimal("0.05"), 10)["discount_amount"] == "0.01"


@pytest.mark.parametrize(
    "amount,percent",
    [(-1, 6), (1, -1), (1, 101), ("NaN", 1), (1, "Infinity"), (1_000_000_001, 1), (True, 5)],
)
def test_invalid_quotes_rejected(amount, percent):
    with pytest.raises(ValueError):
        calculate_discount_quote(amount, percent)


def test_builtin_tool_registered_only_for_granted_accounts_and_revocation_is_live():
    context = UserContext(user_id="u", email="u@local", tenant_id="secret")
    save_resource_policy("tenant", "secret", "secret", False, False, False, "test")
    runner = AgenticRunner()
    deps = AgentDeps(context)
    assert not runner._workspace_toolset(deps).tools
    save_account_policy(context.email, {"allowed_tool_ids": [DISCOUNT_TOOL_ID]}, "test")
    toolset = runner._workspace_toolset(deps)
    assert "calculate_discount_quote" in toolset.tools
    callback = toolset.tools["calculate_discount_quote"].function
    deps.ledger.prompt = "按制度计算订单优惠金额"
    deps.ledger.add(
        "db", [{"total_amount": 2000}], sql="SELECT SUM(amount) AS total_amount FROM sales"
    )
    deps.ledger.add("kb", [{"content": "折扣上限6%。"}])
    result = asyncio.run(callback())
    assert result["quote_amount"] == "1880.00"
    assert deps.ledger.entries[-1]["kind"] == "tool"
    save_account_policy(context.email, {"allowed_tool_ids": []}, "test")
    refused = asyncio.run(callback())
    assert "error" in refused


def test_agent_passes_verified_user_to_file_tool(monkeypatch):
    from src.agent import core_tools

    seen = {}

    def inspect(path, **kw):
        """Provide inspect test data or behavior for this scenario."""
        seen.update(kw)
        return {"content": "own file"}

    monkeypatch.setattr(core_tools, "inspect_temporary_file", inspect)
    runner = AgenticRunner()
    agent = runner._build_agent()
    deps = AgentDeps(UserContext(user_id="verified-user", email="u@local", tenant_id=""))

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        with agent.override(
            model=TestModel(call_tools=["inspect_local_file"], custom_output_text="Read own file.")
        ):
            return await agent.run("Read my file", deps=deps)

    asyncio.run(run())
    assert seen["user_id"] == "verified-user"


def test_file_paths_are_account_confined(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_ROOT_DIR", str(tmp_path))
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()
    (tmp_path / "alice" / "own.txt").write_text("alice text", encoding="utf8")
    (tmp_path / "bob" / "private.txt").write_text("bob secret", encoding="utf8")
    own = inspect_temporary_file("own.txt", user_id="alice")
    assert "error" not in own
    assert "error" in inspect_temporary_file("../bob/private.txt", user_id="alice")
    assert "error" in inspect_temporary_file(str(tmp_path / "bob" / "private.txt"), user_id="alice")
    assert "error" in inspect_temporary_file("own.txt", user_id="..")
    assert "error" in inspect_temporary_file(str(settings.METADATA_PATH), user_id="alice")


def test_file_tool_rejects_oversized_context(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_ROOT_DIR", str(tmp_path))
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "huge.txt").write_text("x" * 20_000, encoding="utf8")
    assert "context budget" in inspect_temporary_file("huge.txt", user_id="alice")["error"]
