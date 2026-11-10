"""Reserve four Evals splits before results; extra counterexamples cannot replace the 900-item holdout."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evals.quality.prepare import Corpus, prepare_cmrc, prepare_legal, prepare_pubmed
from evals.quality.records import jsonl_line, read_jsonl


class FrozenCorpus(Corpus):
    def __init__(self, dataset: Path):
        self.cases = []
        self.sources = json.loads((dataset / "sources.json").read_text("utf-8"))
        self.expected = {s["id"]: s for s in self.sources}
        self._ids = set()
        self.source_groups = {}

    def source(self, *, identifier, group, data, **_):
        if (
            identifier in self._ids
            or hashlib.sha256(data).hexdigest() != self.expected[identifier]["sha256"]
        ):
            raise ValueError("Auxiliary cases do not match the existing frozen original corpus")
        self._ids.add(identifier)
        self.source_groups[identifier] = group


def freeze(dataset: Path, legal: Path) -> list[dict]:
    cases = [
        row
        for split in ("train", "selection", "holdout")
        for row in read_jsonl(dataset / (split + ".jsonl"))
    ]
    used_groups = {row["group"] for row in cases}
    corpus = FrozenCorpus(dataset)
    prepare_cmrc(corpus, json.loads((dataset / "raw/cmrc-dev.json").read_text("utf-8-sig")))
    prepare_pubmed(
        corpus, json.loads((dataset / "raw/pubmedqa-labeled.json").read_text("utf-8-sig"))
    )
    prepare_legal(corpus, legal)
    for cohort in ("zh-general", "en-medical", "en-legal"):
        available = sorted(
            (
                c
                for c in corpus.cases
                if c["cohort"] == cohort
                and c["group"] not in used_groups
                and c["split"] == "holdout"
            ),
            key=lambda c: hashlib.sha256(("aux-counterexample-v1:" + c["id"]).encode()).hexdigest(),
        )
        if not available:
            raise ValueError("No independent auxiliary counterexample source remains")
        case = dict(
            available[0],
            split="counterexample",
            original_partition="unused-holdout-reserve",
            usage="Auxiliary original case; excluded from formal 900-question quality holdout",
        )
        used_groups.add(case["group"])
        cases.append(case)
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--legal-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = freeze(args.dataset, args.legal_archive)
    with args.output.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(jsonl_line(row))
    print(
        json.dumps(
            {
                "cases": len(rows),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "holdout_results_inspected": False,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
