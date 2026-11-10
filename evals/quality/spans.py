"""Check each fixed Parent unit actually returned against official source spans."""

from __future__ import annotations

import hashlib
import json
import re
from array import array
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from evals.quality.metrics import ranking_metrics


def unit_id(source_id: str, text: str) -> str:
    return hashlib.sha256(json.dumps([source_id, text], ensure_ascii=False).encode()).hexdigest()


def searchable_source(text: str) -> tuple[str, array]:
    """Normalize only whitespace; retain original character offsets in a compact integer array."""
    pieces = []
    positions = array("I")
    previous_end = 0
    for match in re.finditer(r"\S+", text):
        if pieces:
            pieces.append(" ")
            positions.append(previous_end)
        pieces.append(match.group())
        positions.extend(range(match.start(), match.end()))
        previous_end = match.end()
    return "".join(pieces), positions


def locations(fragment: str, normalized: str, positions: array) -> list[tuple[int, int]]:
    query = " ".join(fragment.split())
    if not query:
        return []
    found = []
    start = normalized.find(query)
    while start >= 0:
        found.append((positions[start], positions[start + len(query) - 1] + 1))
        start = normalized.find(query, start + 1)
    return found


class SpanScorer:
    def __init__(self, sources: dict[str, str], parents: list[dict]):
        self.sources = sources
        self.parents = {}
        self.by_source = defaultdict(list)
        for parent in parents:
            key = unit_id(parent["source_id"], parent["text"])
            if key != parent["id"]:
                raise ValueError("Frozen canonical parent identity changed")
            self.parents[key] = parent
            self.by_source[parent["source_id"]].append(key)

    @classmethod
    def from_files(cls, dataset: Path, catalogue: Path) -> SpanScorer:
        sources = json.loads((dataset / "sources.json").read_text("utf-8"))
        raw = {}
        for source in sources:
            data = (dataset / source["file"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != source["sha256"]:
                raise ValueError("Public original bytes differ from the frozen corpus")
            raw[source["id"]] = data.decode("utf-8")
        return cls(raw, json.loads(catalogue.read_text("utf-8")))

    @lru_cache(maxsize=64)
    def _source(self, source_id: str):
        return searchable_source(self.sources[source_id])

    @lru_cache(maxsize=16384)
    def _locations(self, key: str):
        parent = self.parents[key]
        return locations(parent["text"], *self._source(parent["source_id"]))

    def score(self, case: dict, response: dict, returned_source_ids: list[str]) -> dict:
        result = {
            "scores": {},
            "errors": {},
            "source_group": case["group"],
            "cohort": case["cohort"],
            "id": case["id"],
            "split": case["split"],
        }
        texts = response["documents"][0]
        if len(texts) > 10 or len(texts) != len(returned_source_ids):
            raise ValueError("Wrong K or malformed source identity receipt")
        returned = [
            unit_id(source, text) for source, text in zip(returned_source_ids, texts, strict=True)
        ]
        if any(key not in self.parents for key in returned):
            result["errors"] = {
                m: "returned-unit-not-in-frozen-parent-catalogue"
                for m in ("recall_at_10", "mrr", "ndcg_at_10")
            }
            return result
        gold = set()
        for span in case.get("spans", []):
            text = self.sources[span["source_id"]]
            start, end = span["start"], span["end"]
            original = text[start:end]
            if original != span["text"]:
                raise ValueError("Official source interval and archived original differ")
            start += len(original) - len(original.lstrip())
            end -= len(original) - len(original.rstrip())
            if start < end:
                gold.add((span["source_id"], start, end))
        if not gold and case.get("answer_type") != "research-qa":
            result["errors"] = {
                m: "official-reference-has-no-verifiable-source-span"
                for m in ("recall_at_10", "mrr", "ndcg_at_10")
            }
            return result
        qrels, covered = {}, set()
        ambiguous = False
        for source in case["source_ids"]:
            for key in self.by_source[source]:
                if not gold:
                    qrels[key] = 1
                    continue
                intervals = self._locations(key)
                verdicts = {
                    any(path == source and a <= start and end <= b for path, start, end in gold)
                    for a, b in intervals
                }
                if not intervals or len(verdicts) != 1:
                    ambiguous = True
                qrels[key] = int(verdicts == {True})
        if ambiguous:
            result["errors"] = {
                m: "ambiguous-or-unmapped-canonical-source-interval"
                for m in ("recall_at_10", "mrr", "ndcg_at_10")
            }
            return result
        for key in returned:
            parent = self.parents[key]
            intervals = self._locations(key)
            for path, start, end in gold:
                if (
                    parent["source_id"] == path
                    and intervals
                    and all(a <= start and end <= b for a, b in intervals)
                ):
                    covered.add((path, start, end))
        if any(qrels.values()):
            ranked = ranking_metrics(returned, qrels)
        else:
            ranked = {"recall_at_10": 0.0, "mrr": 0.0, "ndcg_at_10": 0.0}
        result["unit_recall_at_10"] = ranked["recall_at_10"]
        if gold:
            ranked["recall_at_10"] = (
                float(bool(covered))
                if case.get("span_policy") == "any-official-alternative"
                else len(covered) / len(gold)
            )
        result["scores"] = ranked
        result["native_positive_units"] = sum(qrels.values())
        result["official_spans"] = len(gold)
        result["complete_spans_covered"] = len(covered)
        result["recall_scope"] = (
            "complete-official-span" if gold else "provided-research-context-association"
        )
        return result
