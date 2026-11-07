"""在公开验收规则产生通过分数之前先做校验。

只接受 evaluators/runner 实际会用到的键。数据集存储用的元数据由服务端提供，
绝不来自管理员填写的 expected JSON。
"""

from __future__ import annotations

import math
from typing import Any

LIST_RULES = {
    "required_tools",
    "forbidden_tools",
    "answer_contains",
    "answer_excludes",
    "expected_sources",
}
RULE_KEYS = LIST_RULES | {"expected_rows", "must_deny"}
ALLOWED_KEYS = RULE_KEYS | {"model_id"}
# 这些名称是 AgenticRunner/build_workspace_toolset 注册的工具。新增工具需要
# 显式更新验收规则；拼错的禁用工具名不能让安全回归悄悄通过。
TOOL_NAMES = {
    "plan_analysis",
    "get_business_schema",
    "query_database",
    "search_knowledge_documents",
    "search_web_engine",
    "inspect_local_file",
    "export_analysis",
    "recall_memory",
    "calculate_discount_quote",
}


def _validate_list_rule(name: str, values: Any) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError(f"{name} 必须是包含 1 至 100 项的非空字符串数组")
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 1000 for value in values
    ):
        raise ValueError(f"{name} 每项必须是非空字符串，最长 1000 字符")
    values = [value.strip() for value in values]
    if len({value.casefold() for value in values}) != len(values):
        raise ValueError(f"{name} 不能包含重复规则")
    if name in {"required_tools", "forbidden_tools"} and set(values) - TOOL_NAMES:
        raise ValueError(
            f"{name} 包含未注册的工具名: " + ", ".join(sorted(set(values) - TOOL_NAMES))
        )
    return values


def _validate_rows(rows: Any) -> None:
    # [] 表示断言 SQL 确实返回零行，不是空断言；
    # 上面的其他列表规则不能用 [] 来悄悄关闭检查。
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("expected_rows 必须是至多 100 行的 JSON 对象数组")
    for row in rows:
        if not isinstance(row, dict) or not 1 <= len(row) <= 50:
            raise ValueError("expected_rows 每行必须是包含 1 至 50 列的非空 JSON 对象")
        for column, value in row.items():
            if not isinstance(column, str) or not column.strip() or len(column) > 150:
                raise ValueError("expected_rows 列名必须是非空字符串，最长 150 字符")
            if value is not None and type(value) not in {str, int, float, bool}:
                raise ValueError("expected_rows 只支持字符串、有限数字、布尔值和 null 单元格")
            if (isinstance(value, float) and not math.isfinite(value)) or (
                isinstance(value, str) and len(value) > 1000
            ):
                raise ValueError("expected_rows 单元格必须是有限数字或至多 1000 字符的字符串")


def validate_expected(expected: Any) -> dict[str, Any]:
    """返回校验后的规则；拒绝未知、无效果或格式错误的期望。"""
    if not isinstance(expected, dict):
        raise ValueError("expected 必须是 JSON 对象")
    if any(not isinstance(key, str) for key in expected):
        raise ValueError("expected 规则字段名必须是字符串")
    unknown = set(expected) - ALLOWED_KEYS
    if unknown:
        raise ValueError("未知的 expected 规则字段: " + ", ".join(sorted(unknown)))
    result = dict(expected)
    for name in LIST_RULES & expected.keys():
        result[name] = _validate_list_rule(name, expected[name])
    if "must_deny" in expected and type(expected["must_deny"]) is not bool:
        raise ValueError("must_deny 必须是 JSON 布尔值 true 或 false")
    if "model_id" in expected:
        model_id = expected["model_id"]
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 150:
            raise ValueError("model_id 必须是非空字符串，最长 150 字符")
        result["model_id"] = model_id.strip()
    if "expected_rows" in expected:
        _validate_rows(expected["expected_rows"])
    if set(result.get("required_tools", [])) & set(result.get("forbidden_tools", [])):
        raise ValueError("同一个工具不能同时要求调用和禁止调用")
    if {value.casefold() for value in result.get("answer_contains", [])} & {
        value.casefold() for value in result.get("answer_excludes", [])
    }:
        raise ValueError("同一段答案不能同时要求包含和排除")
    if not (
        LIST_RULES & result.keys() or "expected_rows" in result or result.get("must_deny") is True
    ):
        raise ValueError(
            "expected 至少需要一项有效验收规则；model_id 或 must_deny=false 不构成验收"
        )
    return result
