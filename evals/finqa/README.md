# Public financial-domain retrieval check

This adapter uses **Ragas 0.4.3 IDBasedContextRecall** against the actual running
retrieval service. It does not implement its own ranking metric. It sends one
batch of ten queries through the configured Dense/BM42/RRF/BGE/Parent pipeline.
It neither invokes a cloud judge nor uploads results to an evaluation platform.

The 2026-11-02 fixture is stored in `D:/NexRAG-Acceptance/finqa`. Its manifest
records the official [FinQA](https://github.com/czyssrs/FinQA) test split at commit
`0f16e2867befa6840783e58be38c9efb9229d742`, SHA256 and the first ten unique source
report filenames in published order. Selection and the 0.9 minimum document
recall were fixed before running. Original reference answers/programs are kept
separately from uploaded report text. The upstream license is retained there.

Install evaluation tools in an isolated environment; application dependencies
remain unchanged:

```powershell
uv venv --python 3.12 .local-run/acceptance/ragas-venv
uv pip install --python .local-run/acceptance/ragas-venv/Scripts/python.exe ragas==0.4.3 langchain-community==0.3.31
.local-run/acceptance/ragas-venv/Scripts/python.exe evals/finqa/prepare.py D:/NexRAG-Acceptance/finqa
.local-run/acceptance/ragas-venv/Scripts/python.exe evals/finqa/retrieval.py --fixtures D:/NexRAG-Acceptance/finqa --library a7185438-4496-41a7-a873-f5765c35425b
```

Use a dedicated library containing only the ten public report files. The service
key is read locally from `.env` and is never written into reports. On another
installation, prepare the same manifest/corpus, upload through that instance's
normal knowledge UI and supply its new library ID. The reference report identity
is part of every query because FinQA supplies a report with each question.

Version compatibility: Ragas 0.4.3 imports the old VertexAI module on startup;
langchain-community 0.4.2 removed it, so 0.3.31 is pinned here. In this version the
documented collections import for IDBasedContextRecall is absent, while the
legacy metric import works. This affects only the optional evaluation environment.

`retrieval-results.json` reports **document identity recall**, not supporting-span
recall, answer correctness, financial calculation accuracy, absence of irrelevant
passages or whole-FinQA quality. The separate real frontend answer must still be
checked against the published reference. Do not mark F20 complete from this
retrieval score alone.

For Promptfoo answer checks, use `file://final-answer.cjs` with the existing
`answer_pattern` variable. It matches one complete `FINAL_ANSWER:` line and
rejects a correct number followed by a denial or a different conclusion. The
previous substring assertion could falsely accept "111.97 percentage points
is not valid". Preserve its historical results, but do not use those numeric
pass counts as complete-answer acceptance. The Echo provider can regrade saved
outputs without invoking a model; label such runs as replay, not new answers.
Exact-answer checks still need source, metric, period and unit review.

Official [Ragas context recall documentation](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/context_recall/).
