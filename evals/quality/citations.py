"""Check actual judge-extracted facts and local citations against this run's source evidence."""

from __future__ import annotations

import re

from evals.quality.spans import locations, searchable_source


def validate_audit(
    audit: dict, answer: str, passages: list[dict], originals: dict[str, str]
) -> dict:
    available = {p["id"]: p for p in passages}
    actual_ids = {p["evidence_id"].upper() for p in passages}
    declared = {value.upper() for value in re.findall(r"\[(KB\d+)\]", answer, flags=re.I)}
    fabricated = sorted(declared - actual_ids)
    checked = []
    for claim in audit["claims"]:
        excerpt = claim["answer_excerpt"]
        bound = bool(excerpt) and excerpt in answer and answer.count(excerpt) == 1
        markers = {value.upper() for value in re.findall(r"\[(KB\d+)\]", excerpt, flags=re.I)}
        quotes = []
        for citation in claim["citations"]:
            passage = available.get(citation["passage_id"])
            quote = citation["quote"]
            verified = False
            offsets = []
            if passage and quote and quote in passage["text"] and passage.get("authorized") is True:
                original = originals.get(passage["source_id"], "")
                source, positions = searchable_source(original)
                parent_locations = locations(passage["text"], source, positions)
                quote_locations = locations(quote, source, positions)
                offsets = [
                    (a, b)
                    for a, b in quote_locations
                    if any(start <= a and b <= end for start, end in parent_locations)
                ]
                verified = len(parent_locations) == len(offsets) == 1 and bool(
                    passage.get("ingest_version")
                )
            quotes.append(
                {
                    **citation,
                    "source_quote_verified": verified,
                    "original_offsets": offsets,
                    "local_citation_verified": bool(
                        verified and bound and passage["evidence_id"].upper() in markers
                    ),
                }
            )
        support = (
            bound
            and claim["supported"] == "yes"
            and any(q["source_quote_verified"] for q in quotes)
        )
        cited = support and any(q["local_citation_verified"] for q in quotes)
        checked.append(
            {
                **claim,
                "answer_span_bound": bound,
                "verified_quotes": quotes,
                "support_verified": support,
                "citation_verified": cited,
            }
        )
    complete = audit.get("claims_complete") is True
    substantive = audit.get("substantively_answers") is True
    known_bad = bool(fabricated) or any(c["supported"] == "no" for c in checked)
    all_supported = bool(checked) and all(c["support_verified"] for c in checked)
    hallucination = 1.0 if known_bad else (0.0 if complete and all_supported else None)
    return {
        "claims": checked,
        "claims_complete": complete,
        "substantively_answers": substantive,
        "fabricated_citation_ids": fabricated,
        "hallucination_rate": hallucination,
        "groundedness": float(substantive and complete and all_supported and not fabricated),
        "citation_accuracy": float(
            bool(checked)
            and complete
            and not fabricated
            and all(c["citation_verified"] for c in checked)
        ),
        "boundary": "Semantic entailment and exhaustive extraction are assessed by the named LLM judge, not independent humans",
    }
