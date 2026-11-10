"""Backfill the derived entity graph from an existing Qdrant collection."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    from qdrant_client import QdrantClient

    from src.rag.entity_graph import (
        ScopedEntityGraph,
        backfill,
        extractor_version,
        verify_collection,
    )

    graph = ScopedEntityGraph(args.graph)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    def report(progress):
        receipt = {
            **progress,
            "phase": "running",
            "seconds": time.perf_counter() - started,
            "extractor": extractor_version(),
            "corpus": args.collection,
        }
        temporary = args.receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(args.receipt)
        if progress["scanned"] % 1024 == 0:
            print(json.dumps(receipt, ensure_ascii=False), flush=True)

    client = QdrantClient(
        url=args.url, api_key=os.environ.get("QDRANT_API_KEY") or None, timeout=120
    )
    try:
        result = backfill(client, args.collection, graph, report=report)
        if args.verify:
            result["evidence_verification"] = verify_collection(client, args.collection, graph)
    finally:
        client.close()
    result.update(
        phase="done",
        seconds=time.perf_counter() - started,
        extractor=extractor_version(),
        corpus=args.collection,
        stats=graph.stats(args.collection),
    )
    args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.verify and not result["evidence_verification"]["passed"]:
        raise RuntimeError("Entity graph original-source verification failed")


if __name__ == "__main__":
    main()
