"""真实回执适配器不把参考答案或其他轮次证据填入模型上下文。"""

import hashlib
import json

from evals.quality.generation_inputs import load_inputs


def fixture(tmp_path, *, returns=None, trace_id="response-1", fallback=False):
    dataset, run = tmp_path / "dataset", tmp_path / "run"
    dataset.mkdir()
    run.mkdir()
    (run / "model-contexts").mkdir()
    raw = b"Public source statement."
    (dataset / "original.txt").write_bytes(raw)
    (dataset / "sources.json").write_text(
        json.dumps(
            [{"id": "source", "file": "original.txt", "sha256": hashlib.sha256(raw).hexdigest()}]
        ),
        "utf-8",
    )
    (dataset / "train.jsonl").write_text(
        json.dumps(
            {
                "id": "case",
                "cohort": "zh-general",
                "group": "g",
                "query": "Question?",
                "reference": "REFERENCE MUST NEVER BECOME EVIDENCE",
            }
        )
        + "\n",
        "utf-8",
    )
    (run / "uploads.json").write_text(json.dumps({"source": {"file_id": "upload"}}), "utf-8")
    (run / "authorization.json").write_text(
        json.dumps({"knowledge_scopes": ["authorized"]}), "utf-8"
    )
    response = {
        "id": "response-1",
        "choices": [{"message": {"content": "Answer."}, "finish_reason": "stop"}],
    }
    (run / "train-chat.jsonl").write_text(
        json.dumps({"id": "case", "request_id": "request-1", "status": 200, "response": response})
        + "\n",
        "utf-8",
    )
    trace = {
        "trace_id": trace_id,
        "status": "success",
        "model_calls": 1,
        "rule_scores": {"grounding_fallback": int(fallback)},
        "tool_returns": returns or [],
    }
    key = hashlib.sha256(trace_id.encode()).hexdigest()
    (run / "model-contexts" / (key + ".json")).write_text(json.dumps(trace), "utf-8")
    return dataset, run


def test_no_retrieval_does_not_get_reference_answer_as_context(tmp_path):
    row = load_inputs(*fixture(tmp_path), "train")[0]
    assert not row["errors"]
    assert row["passages"] == [] and row["originals"] == {}
    assert "REFERENCE" in row["reference"]


def test_other_turn_trace_is_not_substituted(tmp_path):
    row = load_inputs(*fixture(tmp_path, trace_id="other-turn"), "train")[0]
    assert row["errors"]["generation_input"]["kind"] == "FileNotFoundError"
    assert row["passages"] == []


def test_trace_contents_must_match_current_turn_even_if_filename_matches(tmp_path):
    dataset, run = fixture(tmp_path)
    path = run / "model-contexts" / (hashlib.sha256(b"response-1").hexdigest() + ".json")
    captured = json.loads(path.read_text("utf-8"))
    captured["trace_id"] = "unrelated-old-turn"
    path.write_text(json.dumps(captured), "utf-8")
    row = load_inputs(dataset, run, "train")[0]
    assert "another turn" in row["errors"]["generation_input"]["reason"]


def test_authorized_actual_tool_source_and_fallback_origin_remain_distinct(tmp_path):
    source_id = hashlib.sha256(b"upload:authorized").hexdigest()
    returns = [
        {
            "content": {
                "evidence_id": "KB1",
                "documents": [
                    {
                        "content": "Public source statement.",
                        "metadata": {
                            "file_id": source_id,
                            "collection_name": "authorized",
                            "ingest_version": "v1",
                        },
                    }
                ],
            }
        }
    ]
    row = load_inputs(*fixture(tmp_path, returns=returns, fallback=True), "train")[0]
    assert row["passages"][0]["authorized"] is True
    assert row["passages"][0]["ingest_version"] == "v1"
    assert row["answer_origin"] == "application-grounding-fallback"
    assert row["passages"][0]["text"] != row["reference"]


def test_unmapped_private_or_previous_version_source_is_an_error(tmp_path):
    returns = [
        {
            "content": {
                "evidence_id": "KB1",
                "documents": [
                    {
                        "content": "Unknown source.",
                        "metadata": {
                            "file_id": "not-from-this-public-upload",
                            "collection_name": "private",
                        },
                    }
                ],
            }
        }
    ]
    row = load_inputs(*fixture(tmp_path, returns=returns), "train")[0]
    assert row["errors"]["generation_input"]["kind"] == "ValueError"
