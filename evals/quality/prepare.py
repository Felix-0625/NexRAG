"""Freeze public sources, questions and source-isolated splits; keep answers out of uploads."""

from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import httpx

from evals.quality.records import jsonl_line

CMRC_REV = "c0eb1b6ba219847457e6af3180da722bbeb656af"
PUBMED_REV = "1cbae8e92f72f20c8d3747cbb3bf5bc53554d997"
LEGAL_SHA = "27431be37db9b1db23f8ab790a42d076adb1f72d7f9e7562e36a10573405f88d"
SEED = "nexrag-quality-independent-v1-20261109"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ordered_key(identifier: str) -> str:
    return digest((SEED + ":" + identifier).encode())


def partition(source_group: str) -> str:
    bucket = int(ordered_key(source_group)[:8], 16) % 100
    return "train" if bucket < 20 else "selection" if bucket < 30 else "holdout"


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def download(client: httpx.Client, url: str, target: Path, limit: int = 25_000_000) -> dict:
    """Keep raw server UTF-8 bytes; reject error pages, oversized data and decoding replacements."""
    with client.stream("GET", url) as response:
        response.raise_for_status()
        raw = bytearray()
        for block in response.iter_bytes():
            raw.extend(block)
            if len(raw) > limit:
                raise ValueError("Official resource exceeded its frozen size bound")
    data = bytes(raw)
    text = data.decode("utf-8-sig")
    if "\ufffd" in text:
        raise ValueError("Replacement characters in official source; inspect encoding")
    target.write_bytes(data)
    return {"url": url, "file": target.name, "sha256": digest(data), "bytes": len(data)}


class Corpus:
    def __init__(self, output: Path):
        self.output = output
        self.sources: list[dict] = []
        self.cases: list[dict] = []
        self._ids: set[str] = set()
        self.source_groups: dict[str, str] = {}
        (output / "upload").mkdir()

    def source(
        self,
        *,
        identifier: str,
        group: str,
        cohort: str,
        data: bytes,
        origin: str,
        upload_name: str | None = None,
    ) -> None:
        if identifier in self._ids:
            raise ValueError("Duplicate public source identity")
        self._ids.add(identifier)
        self.source_groups[identifier] = group
        data.decode("utf-8-sig")
        # Local paths are bounded hashes; HTTP upload names preserve legitimate
        # published metadata, never question/reference labels.
        filename = digest(identifier.encode())[:24] + ".txt"
        directory = self.output / "upload" / cohort
        directory.mkdir(exist_ok=True)
        (directory / filename).write_bytes(data)
        self.sources.append(
            {
                "id": identifier,
                "group": group,
                "cohort": cohort,
                "file": f"upload/{cohort}/{filename}",
                "sha256": digest(data),
                "bytes": len(data),
                "origin": origin,
                "upload_name": upload_name or filename,
            }
        )

    def case(
        self,
        *,
        identifier: str,
        group: str,
        cohort: str,
        query: str,
        reference: str,
        source_ids: list[str],
        spans: list[dict] | None = None,
        **extra: object,
    ) -> None:
        if not query.strip() or not reference.strip() or not source_ids:
            raise ValueError("Missing original question, reference or source association")
        self.cases.append(
            {
                "id": identifier,
                "group": group,
                "cohort": cohort,
                "split": partition(group),
                "query": query,
                "reference": reference,
                "source_ids": source_ids,
                "spans": spans or [],
                **extra,
            }
        )


def prepare_cmrc(corpus: Corpus, document: dict) -> None:
    for article in document["data"]:
        title = article.get("title", "")
        # All published contexts are indexed, including contexts with no selected
        # questions. No span, answer or question controls source segmentation.
        for paragraph in article["paragraphs"]:
            text = paragraph["context"]
            source_id = "cmrc:" + digest(text.encode())
            group = "cmrc-article:" + (title or source_id)
            if source_id not in corpus._ids:
                corpus.source(
                    identifier=source_id,
                    group=group,
                    cohort="zh-general",
                    data=text.encode(),
                    origin="CMRC2018 official dev published context",
                    upload_name=(title or source_id.replace(":", "-")) + ".txt",
                )
            group = corpus.source_groups[source_id]
            for question in paragraph["qas"]:
                spans = []
                annotation_issues = []
                for answer in question["answers"]:
                    start = int(answer["answer_start"])
                    value = answer["text"]
                    if start < 0 or text[start : start + len(value)] != value:
                        # The official release includes non-extractive references
                        # with answer_start=-1. Preserve them instead of guessing
                        # a span or selecting an easier replacement question.
                        annotation_issues.append(
                            {
                                "answer_start": start,
                                "text": value,
                                "reason": "official-reference-has-no-verifiable-span",
                            }
                        )
                        continue
                    spans.append(
                        {
                            "source_id": source_id,
                            "start": start,
                            "end": start + len(value),
                            "text": value,
                        }
                    )
                corpus.case(
                    identifier="cmrc:" + question["id"],
                    group=group,
                    cohort="zh-general",
                    query=question["question"],
                    reference=question["answers"][0]["text"],
                    source_ids=[source_id],
                    spans=spans,
                    reference_alternatives=[a["text"] for a in question["answers"]],
                    answer_type="span",
                    span_policy="any-official-alternative",
                    annotation_issues=annotation_issues,
                    span_evidence_available=bool(spans),
                )


def prepare_pubmed(corpus: Corpus, document: dict) -> None:
    for pmid, item in document.items():
        source_id = "pubmedqa:" + pmid
        # QUESTION and LONG_ANSWER stay exclusively in the scoring case file.
        # The benchmark context intentionally omits the abstract conclusion.
        # Record that limitation; never add the answer to make grounding pass.
        contexts = item["CONTEXTS"]
        text = "\n\n".join(contexts)
        corpus.source(
            identifier=source_id,
            group=source_id,
            cohort="en-medical",
            data=text.encode(),
            origin=f"PubMedQA PQA-L provided abstract context PMID {pmid}",
        )
        corpus.case(
            identifier=source_id,
            group=source_id,
            cohort="en-medical",
            query=item["QUESTION"],
            reference=item["LONG_ANSWER"],
            source_ids=[source_id],
            decision=item["final_decision"],
            answer_type="research-qa",
            evidence_limit="Provided contexts omit conclusions; reference support may be incomplete",
        )


def prepare_legal(corpus: Corpus, path: Path) -> None:
    if digest(path.read_bytes()) != LEGAL_SHA:
        raise ValueError("Official LegalBench-RAG archive hash differs")
    with ZipFile(path) as archive:
        names = sorted(
            n for n in archive.namelist() if n.startswith("corpus/cuad/") and n.endswith(".txt")
        )
        if len(names) != 462:
            raise ValueError("Expected the entire 462-source CUAD corpus")
        sources = {}
        for name in names:
            source_id = "legalbench:" + unicodedata.normalize("NFC", name.removeprefix("corpus/"))
            data = archive.read(name)
            sources[source_id] = data.decode("utf-8")
            corpus.source(
                identifier=source_id,
                group=source_id,
                cohort="en-legal",
                data=data,
                origin=name,
                upload_name=PurePosixPath(name).name,
            )
        tests = json.loads(archive.read("benchmarks/cuad.json"))["tests"]
        for index, case in enumerate(tests):
            ids = sorted(
                {
                    "legalbench:" + unicodedata.normalize("NFC", s["file_path"])
                    for s in case["snippets"]
                }
            )
            if len(ids) != 1:
                raise ValueError(
                    "CUAD source-disjoint split needs a connected-component grouping for cross-file cases"
                )
            spans = []
            for item in case["snippets"]:
                source_id = "legalbench:" + unicodedata.normalize("NFC", item["file_path"])
                start, end = item["span"]
                if sources[source_id][start:end] != item["answer"]:
                    raise ValueError("LegalBench official evidence differs from original bytes")
                spans.append(
                    {"source_id": source_id, "start": start, "end": end, "text": item["answer"]}
                )
            corpus.case(
                identifier=f"legalbench-cuad:{index}",
                group=ids[0],
                cohort="en-legal",
                query=case["query"],
                reference="\n\n".join(s["text"] for s in spans),
                source_ids=ids,
                spans=spans,
                answer_type="clause-extraction",
                span_policy="all-distinct-official-evidence-spans",
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legal-archive", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    raw = args.output / "raw"
    raw.mkdir()
    resources = []
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for repository, revision, filename, local in (
            ("ymcui/cmrc2018", CMRC_REV, "squad-style-data/cmrc2018_dev.json", "cmrc-dev.json"),
            ("ymcui/cmrc2018", CMRC_REV, "LICENCE", "cmrc-license.txt"),
            ("pubmedqa/pubmedqa", PUBMED_REV, "data/ori_pqal.json", "pubmedqa-labeled.json"),
            ("pubmedqa/pubmedqa", PUBMED_REV, "LICENSE", "pubmedqa-license.txt"),
        ):
            url = f"https://raw.githubusercontent.com/{repository}/{revision}/{filename}"
            resources.append(download(client, url, raw / local))
    corpus = Corpus(args.output)
    prepare_cmrc(corpus, json.loads((raw / "cmrc-dev.json").read_text("utf-8-sig")))
    prepare_pubmed(corpus, json.loads((raw / "pubmedqa-labeled.json").read_text("utf-8-sig")))
    prepare_legal(corpus, args.legal_archive)
    if len({c["id"] for c in corpus.cases}) != len(corpus.cases):
        raise ValueError("Duplicate official case identifiers")
    selected = []
    for cohort in ("zh-general", "en-medical", "en-legal"):
        for split, requested in (("train", 60), ("selection", 60), ("holdout", 300)):
            candidates = sorted(
                (c for c in corpus.cases if c["cohort"] == cohort and c["split"] == split),
                key=lambda c: ordered_key(c["id"]),
            )
            if split == "holdout":
                # Repeated questions about one source are correlated. Reserve
                # one question per original source group before seeing outcomes.
                unique = {}
                for case in candidates:
                    unique.setdefault(case["group"], case)
                candidates = list(unique.values())
            if len(candidates) < requested:
                raise ValueError(f"Insufficient {cohort}/{split} original cases")
            selected.extend(candidates[:requested])
    for split in ("train", "selection", "holdout"):
        with (args.output / (split + ".jsonl")).open("w", encoding="utf-8") as stream:
            for case in selected:
                if case["split"] == split:
                    stream.write(jsonl_line(case))
    dump(args.output / "sources.json", corpus.sources)
    dump(
        args.output / "manifest.json",
        {
            "protocol_id": "nexrag-quality-v1-20261109",
            "seed": SEED,
            "prepared_by_sha256": digest(Path(__file__).read_bytes()),
            "resources": resources,
            "legal_archive_sha256": LEGAL_SHA,
            "legal_subset": "entire-cuad-corpus",
            "selection": "hash before outcomes, source-group split 20/10/70",
            "path_identity": "Unicode NFC for source IDs only; archived source bytes unchanged",
            "sources": len(corpus.sources),
            "available_original_cases": len(corpus.cases),
            "holdout_sampling": "one hash-selected question per original source group; 300 distinct groups per cohort",
            "annotation_issues": sum(bool(c.get("annotation_issues")) for c in corpus.cases),
            "selected_without_verifiable_span": [
                c["id"] for c in selected if c.get("span_evidence_available") is False
            ],
            "selected_case_ids": {
                s: [c["id"] for c in selected if c["split"] == s]
                for s in ("train", "selection", "holdout")
            },
            "cohorts": {
                g: {
                    "sources": sum(s["cohort"] == g for s in corpus.sources),
                    "selected_cases": {
                        s: sum(c["cohort"] == g and c["split"] == s for c in selected)
                        for s in ("train", "selection", "holdout")
                    },
                }
                for g in ("zh-general", "en-medical", "en-legal")
            },
            "file_sha256": {p.name: digest(p.read_bytes()) for p in args.output.glob("*.jsonl")},
            "sources_sha256": digest((args.output / "sources.json").read_bytes()),
            "contamination": "Excludes all prior ContractNLI/PrivacyQA/MIRACL/HotpotQA questions; public pretraining contamination cannot be excluded",
            "medical_scope": "Biomedical research QA, not a claim of clinical safety; conclusions absent from provided abstracts remain an evidence limitation",
            "holdout_state": "frozen; do not inspect labels or outcomes for candidate tuning",
            "human_review": "pending real independent annotators; no simulated-human replacement",
        },
    )
    print(
        json.dumps(
            {
                "sources": len(corpus.sources),
                "selected": len(selected),
                "output": str(args.output.resolve()),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
