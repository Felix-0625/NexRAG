"""Local Instruct overrides must not affect cloud or thinking models."""

from src.agent.model_settings import agent_model_settings


def test_ollama_instruct_uses_explicit_non_thinking_and_keeps_tool_obligation():
    settings = agent_model_settings(
        {"provider": "ollama", "model_name": "qwen3:4b-instruct"}, {"db"}
    )
    assert settings == {
        "temperature": 0,
        "top_p": 0.8,
        "openai_reasoning_effort": "none",
        "tool_choice": "required",
    }
    assert (
        agent_model_settings({"provider": "ollama", "model_name": "qwen3:4b-instruct"}, set())[
            "tool_choice"
        ]
        == "auto"
    )


def test_cloud_and_thinking_models_do_not_receive_ollama_instruct_override():
    for record in [
        {"provider": "openai", "model_name": "qwen3-instruct"},
        {"provider": "ollama", "model_name": "qwen3:4b-thinking"},
        {},
    ]:
        assert agent_model_settings(record, set()) == {"temperature": 0, "tool_choice": "auto"}
