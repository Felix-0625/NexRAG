"""文件命中、部分区间及重复文字均不能冒充完整证据命中。"""

from evals.quality.spans import SpanScorer, unit_id


def scorer(text, fragments):
    return SpanScorer(
        {"s": text}, [{"id": unit_id("s", f), "source_id": "s", "text": f} for f in fragments]
    )


def case(text, start, end):
    return {
        "id": "q",
        "group": "s",
        "cohort": "en-legal",
        "split": "holdout",
        "source_ids": ["s"],
        "answer_type": "clause-extraction",
        "spans": [{"source_id": "s", "start": start, "end": end, "text": text[start:end]}],
    }


def test_same_file_and_partial_quote_do_not_receive_full_span_credit():
    text = "Header. Complete relevant evidence. Unrelated footer."
    reference = case(text, 8, 35)
    judge = scorer(text, [text, "Header.", "Complete relevant", "Unrelated footer."])
    value = judge.score(reference, {"documents": [["Complete relevant"]]}, ["s"])
    assert value["scores"]["recall_at_10"] == 0
    assert value["scores"]["mrr"] == 0
    assert value["scores"]["ndcg_at_10"] == 0


def test_normalized_whitespace_keeps_original_offsets_and_correct_first_relevant_rank():
    text = "Header.\r\n\r\nExact original evidence.\nFooter."
    a, b = text.index("Exact"), text.index("\nFooter")
    judge = scorer(text, ["Header.", "Exact original evidence."])
    value = judge.score(
        case(text, a, b), {"documents": [["Header.", "Exact original evidence."]]}, ["s", "s"]
    )
    assert value["scores"]["recall_at_10"] == 1
    assert value["scores"]["mrr"] == 0.5
    assert not value["errors"]


def test_repeated_source_text_with_different_annotation_status_is_not_guessed():
    text = "Same clause. Interlude. Same clause."
    judge = scorer(text, ["Same clause."])
    value = judge.score(case(text, 0, 12), {"documents": [["Same clause."]]}, ["s"])
    assert value["scores"] == {}
    assert "ambiguous" in value["errors"]["recall_at_10"]


def test_different_context_unit_catalogue_cannot_be_scored_as_the_frozen_parent_catalogue():
    text = "Original evidence."
    judge = scorer(text, [text])
    value = judge.score(case(text, 0, len(text)), {"documents": [["Modified evidence."]]}, ["s"])
    assert value["scores"] == {}
    assert "frozen-parent" in value["errors"]["ndcg_at_10"]
