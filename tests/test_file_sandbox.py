"""Sandboxed file inspection and security boundaries."""

from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from src.agent.agent import AgentDeps, AgenticRunner, UserContext
from src.agent.tools import inspect_temporary_file
from src.midplatform.sandbox_file import (
    FileSandboxSecurityError,
    inspect_file_content,
    validate_safe_path,
)


def test_path_traversal_prevention(tmp_path: Path):
    """Reject paths resolving outside the allowed root."""
    outside_file = tmp_path.parent / "secret.txt"
    outside_file.write_text("outside data", encoding="utf-8")

    with pytest.raises(FileSandboxSecurityError, match="outside sandbox root"):
        validate_safe_path(outside_file, base_dir=tmp_path)


def test_sandbox_root_is_a_dedicated_directory_not_the_cwd(tmp_path, monkeypatch):
    """Require SANDBOX_ROOT_DIR rather than the working directory.

    Otherwise running outside an empty container directory can expose application source and
    configuration.
    """
    from src.config import settings

    sandbox_dir = tmp_path / "uploads"
    sandbox_dir.mkdir()
    sample = sandbox_dir / "metrics.csv"
    sample.write_text("a,b\n1,2\n", encoding="utf-8")

    # Files outside the sandbox must remain inaccessible
    # even if their directory happens to be the process working directory.
    outside = tmp_path / "config.yaml"
    outside.write_text("secret: value", encoding="utf-8")

    monkeypatch.setattr(settings, "SANDBOX_ROOT_DIR", str(sandbox_dir))
    monkeypatch.chdir(tmp_path)

    result = inspect_temporary_file("metrics.csv")
    assert result["format"] == "tabular"

    denied = inspect_temporary_file(str(outside))
    assert "error" in denied


def test_forbidden_sensitive_patterns(tmp_path: Path):
    """Reject sensitive environment, Git, database, and key files."""
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=123", encoding="utf-8")
    with pytest.raises(FileSandboxSecurityError, match="sensitive path component"):
        validate_safe_path(env_file, base_dir=tmp_path)

    key_file = tmp_path / "private.key"
    key_file.write_text("---KEY---", encoding="utf-8")
    with pytest.raises(FileSandboxSecurityError, match="file extension"):
        validate_safe_path(key_file, base_dir=tmp_path)

    db_file = tmp_path / "data.db"
    db_file.write_bytes(b"sqlite")
    with pytest.raises(FileSandboxSecurityError, match="file extension"):
        validate_safe_path(db_file, base_dir=tmp_path)


def test_inspect_tabular_csv(tmp_path: Path):
    """CSV inspection returns headers, row counts, and samples."""
    csv_file = tmp_path / "orders.csv"
    csv_file.write_text(
        "order_id,amount,status\n"
        "ord_001,150.0,delivered\n"
        "ord_002,230.5,shipped\n"
        "ord_003,99.0,canceled\n",
        encoding="utf-8",
    )

    result = inspect_file_content(csv_file, max_rows=2, base_dir=tmp_path)
    assert result["format"] == "tabular"
    assert result["columns"] == ["order_id", "amount", "status"]
    assert result["total_rows"] == 3
    assert result["sampled_count"] == 2
    assert result["sample_rows"][0]["order_id"] == "ord_001"
    assert result["sample_rows"][1]["amount"] == "230.5"


def test_inspect_json_array_and_object(tmp_path: Path):
    """JSON inspection returns structured array and object summaries."""
    json_array_file = tmp_path / "items.json"
    json_array_file.write_text(
        '[{"id": 1, "name": "item1"}, {"id": 2, "name": "item2"}]',
        encoding="utf-8",
    )
    arr_res = inspect_file_content(json_array_file, max_rows=1, base_dir=tmp_path)
    assert arr_res["format"] == "json_array"
    assert arr_res["total_items"] == 2
    assert arr_res["columns"] == ["id", "name"]
    assert len(arr_res["sample_items"]) == 1

    json_obj_file = tmp_path / "config.json"
    json_obj_file.write_text('{"host": "localhost", "port": 8080}', encoding="utf-8")
    obj_res = inspect_file_content(json_obj_file, base_dir=tmp_path)
    assert obj_res["format"] == "json_object"
    assert "host" in obj_res["top_level_keys"]
    assert "port" in obj_res["top_level_keys"]


def test_inspect_text_file_pagination(tmp_path: Path):
    """Paginate text by line limits to avoid context overflow."""
    log_file = tmp_path / "app.log"
    log_file.write_text("\n".join(f"Line {i}" for i in range(1, 21)), encoding="utf-8")

    result = inspect_file_content(log_file, start_line=5, max_lines=5, base_dir=tmp_path)
    assert result["format"] == "text"
    assert result["total_lines"] == 20
    assert result["start_line"] == 5
    assert result["returned_lines"] == 5
    assert result["has_more"] is True
    assert "Line 5" in result["content"]
    assert "Line 9" in result["content"]
    assert "Line 10" not in result["content"]


@pytest.mark.anyio
async def test_agent_inspect_local_file_tool(monkeypatch):
    """Safe agent file inspection adds one evidence call."""

    def fake_inspect(file_path, **kwargs):
        """Provide fake inspect test data or behavior for this scenario."""
        return {"format": "tabular", "columns": ["metric", "value"], "total_rows": 2}

    from src.agent import core_tools

    monkeypatch.setattr(core_tools, "inspect_temporary_file", fake_inspect)

    runner = AgenticRunner()
    agent = runner._build_agent()

    context = UserContext(user_id="u1", email="test@example.com", tenant_id="t1")
    deps = AgentDeps(context=context)

    # Exercise the registered inspection tool through TestModel.
    test_model = TestModel(call_tools=["inspect_local_file"])
    with agent.override(model=test_model):
        result = await agent.run(
            "Please check metrics.csv",
            deps=deps,
        )
    assert deps.evidence_calls >= 1
    assert result.output
