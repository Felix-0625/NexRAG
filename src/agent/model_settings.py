"""Provider-specific execution defaults for the governed single agent."""


def agent_model_settings(record, missing):
    """Use deterministic sampling and request missing tools, falling back to auto when unsupported."""
    result = {"temperature": 0, "tool_choice": "required" if missing else "auto"}
    # An Ollama alias may advertise thinking even when it targets an Instruct checkpoint.
    # Disable thinking explicitly through OpenAI compatibility; native think:false is not portable here.
    # Generic model-card sampling defaults do not guarantee factual tool execution.
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
            # The ledger still rejects missing results; only unsupported transport-level forcing is relaxed.
            result["tool_choice"] = "auto"
    if record.get("provider") == "ollama" and name.startswith("qwen3") and "instruct" in name:
        result.update(temperature=0, top_p=0.8, openai_reasoning_effort="none")
    return result
