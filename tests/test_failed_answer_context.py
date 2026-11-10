"""Partial-evidence fallbacks must not become completed-answer history."""

from src.agent.context import build_prompt


def test_partial_fallback_and_empty_errors_are_excluded_but_user_request_survives():
    previous = "Which report should I use?"
    fallback = "以下内容由本轮实际工具结果生成。\n[KB1] truncated financial source"
    result = build_prompt(
        "Read the report again", [("user", previous), ("assistant", fallback), ("assistant", "")]
    )
    assert previous in result
    assert "truncated financial source" not in result
    assert "Read the report again" in result


def test_successful_answers_and_user_authored_fallback_text_are_preserved_as_untrusted():
    result = build_prompt(
        "current",
        [
            ("assistant", "Verified answer with source"),
            ("user", "以下内容由本轮实际工具结果生成。 is the message I saw"),
        ],
    )
    assert "Verified answer with source" in result
    assert "is the message I saw" in result
    assert "untrusted context" in result
