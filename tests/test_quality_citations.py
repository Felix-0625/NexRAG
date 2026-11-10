"""引用存在于答案其他位置、旧轮证据或无支持原句均不能过门禁。"""

from evals.quality.citations import validate_audit

PASSAGES = [
    {
        "id": "KB1:0",
        "evidence_id": "KB1",
        "source_id": "s",
        "text": "The deadline is Friday.",
        "authorized": True,
        "ingest_version": "v1",
    }
]
ORIGINALS = {"s": "Header. The deadline is Friday. Footer."}


def audit(excerpt, supported="yes", quote="The deadline is Friday."):
    return {
        "claims_complete": True,
        "substantively_answers": True,
        "claims": [
            {
                "statement": "The deadline is Friday.",
                "answer_excerpt": excerpt,
                "supported": supported,
                "citations": [{"passage_id": "KB1:0", "quote": quote}],
            }
        ],
    }


def test_quote_exists_and_local_current_turn_citation_supports_the_claim():
    answer = "The deadline is Friday. [KB1]"
    result = validate_audit(audit(answer), answer, PASSAGES, ORIGINALS)
    assert result["citation_accuracy"] == 1
    assert result["hallucination_rate"] == 0
    assert result["groundedness"] == 1


def test_unrelated_footer_citation_is_not_local_claim_coverage():
    answer = "The deadline is Friday.\n\nOther sources: [KB1]"
    result = validate_audit(audit("The deadline is Friday."), answer, PASSAGES, ORIGINALS)
    assert result["citation_accuracy"] == 0
    assert result["hallucination_rate"] == 0


def test_fabricated_ids_and_unsupported_claims_are_answer_level_hallucinations():
    answer = "The deadline is Friday. [KB999]"
    result = validate_audit(audit(answer), answer, PASSAGES, ORIGINALS)
    assert result["hallucination_rate"] == 1 and result["citation_accuracy"] == 0
    answer = "The deadline is Friday. [KB1]"
    result = validate_audit(audit(answer, supported="no"), answer, PASSAGES, ORIGINALS)
    assert result["hallucination_rate"] == 1


def test_unverifiable_or_unexhaustive_extraction_never_receives_perfect_citation_credit():
    answer = "The deadline is Friday. [KB1]"
    result = validate_audit(
        audit(answer, quote="invented source quote"), answer, PASSAGES, ORIGINALS
    )
    assert result["hallucination_rate"] is None and result["citation_accuracy"] == 0
    partial = audit(answer)
    partial["claims_complete"] = False
    assert validate_audit(partial, answer, PASSAGES, ORIGINALS)["citation_accuracy"] == 0
