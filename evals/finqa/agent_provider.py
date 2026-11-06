"""Promptfoo adapter for the real Agent over fixed public retrieval contexts.

No frontend or live retrieval is simulated as newly executed: this is an offline
model evaluation using the already recorded retrieval response. No private user
history, memories, business sources or external services are available.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pydantic_ai.messages import ModelMessagesTypeAdapter  # noqa: E402

from src.agent.agent import AgenticRunner  # noqa: E402
from src.agent.dependencies import UserContext  # noqa: E402
from src.agent.prompts import DEFAULT_GUIDANCE  # noqa: E402
from src.agent.run_options import EvaluationRun  # noqa: E402
from src.evaluation.runner import outcome_from_messages  # noqa: E402
from src.evolution.artifacts import default_params  # noqa: E402
from src.midplatform.agent_models import resolve_model  # noqa: E402
from src.midplatform.data_policy import model_is_local  # noqa: E402
from src.midplatform.ops_store import OpsStore, set_ops_store  # noqa: E402

runner = AgenticRunner()


async def call_api(prompt, options, context):
    fixtures = Path(options["config"]["fixtures"])
    if options["config"].get("opsDatabase"):
        set_ops_store(OpsStore(options["config"]["opsDatabase"]))
    model_id = options["config"].get("modelId")
    index = int(context["vars"]["case_index"])
    if not 1 <= index <= 9:
        raise ValueError("This batch covers only the remaining nine fixed public cases")
    if not model_is_local(resolve_model(model_id)):
        raise ValueError(
            "Public benchmark requires the configured local model; cloud calls refused"
        )
    raw = json.loads((fixtures / "retrieval-raw.json").read_text(encoding="utf8"))
    documents = [
        {"content": text, "source": meta.get("source"), "metadata": meta, "score": score}
        for text, meta, score in zip(
            raw["documents"][index], raw["metadatas"][index], raw["distances"][index], strict=True
        )
    ]
    run = EvaluationRun(DEFAULT_GUIDANCE, default_params(), ["search_knowledge_documents"])
    run.knowledge_documents = documents
    user = UserContext(
        user_id="finqa-public-eval",
        email="finqa@local.test",
        tenant_id="",
        knowledge_scopes=["finqa-public-fixture"],
        capture_memory=False,
        model_id=model_id,
    )
    started = time.monotonic()
    try:
        answer = await runner.run(prompt, user, evaluation=run)
    except Exception as error:
        answer = str(error)
    seconds = time.monotonic() - started
    outcome = outcome_from_messages(
        answer, run.messages, latency_ms=seconds * 1000, status=run.status
    )
    record = {
        "case_index": index,
        "status": run.status,
        "answer": answer,
        "seconds": round(seconds, 3),
        "tools": outcome.tools,
        "scope": "offline real-model evaluation over saved public retrieval contexts",
    }
    output_folder = fixtures
    if label := options["config"].get("runLabel"):
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", label):
            raise ValueError("runLabel must be a simple directory name")
        output_folder = fixtures / label
        output_folder.mkdir(exist_ok=True)
    (output_folder / f"agent-case-{index:02d}.json").write_text(
        json.dumps(
            {**record, "messages": json.loads(ModelMessagesTypeAdapter.dump_json(run.messages))},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf8",
    )
    return {"output": json.dumps(record, ensure_ascii=False)}
