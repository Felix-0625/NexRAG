"""受治理的单 Agent 按模型提供方区分的执行默认值。"""


def agent_model_settings(record, missing):
    """返回确定性的模型参数：temperature 为 0，仍缺证据时强制调用工具（提供方不支持强制时回退为 auto），并按 Ollama qwen3 instruct 调整采样。"""
    result = {"temperature": 0, "tool_choice": "required" if missing else "auto"}
    # Ollama 的别名即便指向 Instruct 检查点，也可能声明支持 thinking。
    # 通过其 OpenAI 兼容协议显式要求不启用 thinking；原生的 `think:false` 无法经该端点移植。
    # 保持事实/工具执行路径的确定性；通用模型卡的采样默认值并不保证答案质量。
    name = record.get("model_name", "").lower()
    if record.get("provider") == "deepseek":
        from src.midplatform.agent_models import deepseek_profile

        profile = deepseek_profile(name)
        if profile and (
            not profile.get("supports_forced_tool_choice", True)
            or (
                profile.get("thinking_enabled_by_default", False)
                and not profile.get("supports_forced_tool_choice_with_thinking", True)
            )
        ):
            # 证据 ledger 仍会拒绝缺失的结果；这里只是避开该提供方不支持的传输层强制调用。
            result["tool_choice"] = "auto"
    if record.get("provider") == "ollama" and name.startswith("qwen3") and "instruct" in name:
        result.update(temperature=0, top_p=0.8, openai_reasoning_effort="none")
    return result
