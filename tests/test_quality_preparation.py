"""公开集准备保留原始数据问题，并隔离模型输入和评测标注。"""

import json

from evals.quality.prepare import Corpus, partition, prepare_cmrc, prepare_pubmed
from evals.quality.records import jsonl_line, read_jsonl


def test_cmrc_nonextractive_reference_is_retained_without_fabricated_offset(tmp_path):
    corpus = Corpus(tmp_path)
    prepare_cmrc(
        corpus,
        {
            "data": [
                {
                    "title": "Original title",
                    "paragraphs": [
                        {
                            "context": "The original text.",
                            "qas": [
                                {
                                    "id": "q1",
                                    "question": "Unseen question?",
                                    "answers": [
                                        {"answer_start": -1, "text": "A paraphrased answer."}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    case = corpus.cases[0]
    assert case["reference"] == "A paraphrased answer."
    assert case["spans"] == [] and case["span_evidence_available"] is False
    assert case["annotation_issues"][0]["answer_start"] == -1
    source = corpus.sources[0]
    assert (tmp_path / source["file"]).read_text("utf8") == "The original text."
    assert source["upload_name"] == "Original title.txt"


def test_pubmed_question_and_reference_never_enter_the_uploaded_context(tmp_path):
    corpus = Corpus(tmp_path)
    prepare_pubmed(
        corpus,
        {
            "123": {
                "CONTEXTS": ["Original methods.", "Original results."],
                "QUESTION": "Hidden question?",
                "LONG_ANSWER": "Hidden conclusion.",
                "final_decision": "maybe",
            }
        },
    )
    upload = (tmp_path / corpus.sources[0]["file"]).read_text("utf8")
    assert upload == "Original methods.\n\nOriginal results."
    assert "Hidden" not in upload
    assert corpus.cases[0]["reference"] == "Hidden conclusion."
    assert "omit conclusions" in corpus.cases[0]["evidence_limit"]


def test_same_source_uses_one_split_even_if_published_under_two_titles(tmp_path):
    corpus = Corpus(tmp_path)
    articles = []
    for index in range(2):
        articles.append(
            {
                "title": f"Alias {index}",
                "paragraphs": [
                    {
                        "context": "Same original.",
                        "qas": [
                            {
                                "id": str(index),
                                "question": "Question?",
                                "answers": [{"answer_start": 0, "text": "Same"}],
                            }
                        ],
                    }
                ],
            }
        )
    prepare_cmrc(corpus, {"data": articles})
    assert len(corpus.sources) == 1
    assert corpus.cases[0]["group"] == corpus.cases[1]["group"]
    assert corpus.cases[0]["split"] == corpus.cases[1]["split"]
    assert partition(corpus.cases[0]["group"]) == corpus.cases[0]["split"]
    assert json.loads(json.dumps(corpus.cases))[0]["spans"][0]["text"] == "Same"


def test_unicode_line_separators_inside_official_text_do_not_split_json_records(tmp_path):
    records = [{"query": "Original\u2028question\u0085text", "reference": "A\u2029B"}]
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(records[0], ensure_ascii=False) + "\n", encoding="utf8")
    assert read_jsonl(path) == records
    path.write_text(jsonl_line(records[0]), encoding="utf8")
    assert len(path.read_text("utf8").splitlines()) == 1
    assert read_jsonl(path) == records
