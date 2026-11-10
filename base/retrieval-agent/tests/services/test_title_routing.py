from unittest.mock import AsyncMock

from app.services.title_routing import explicit_title_order


def metadata(title, file_id, collection="authorized", version="v1"):
    return {
        "search_title": title,
        "file_id": file_id,
        "collection_name": collection,
        "ingest_version": version,
    }


def test_explicit_distinctive_source_hint_prioritizes_that_file_and_preserves_all_candidates():
    candidates = [
        metadata(f"Company{index} confidential agreement", str(index)) for index in range(30)
    ]
    original = list(candidates)
    order = explicit_title_order("What does the Company17 agreement require?", candidates)
    assert order[0] == 17
    assert sorted(order) == list(range(30))
    assert [i for i in order if i != 17] == [i for i in range(30) if i != 17]
    assert candidates == original


def test_generic_or_ambiguous_source_hint_preserves_existing_model_order():
    candidates = [metadata(f"Company{index} agreement", str(index)) for index in range(30)]
    assert explicit_title_order("What does the agreement require?", candidates) == list(range(30))
    candidates[17] = metadata("Company3 agreement", "17")
    assert explicit_title_order("What does Company3 require?", candidates) == list(range(30))


def test_sibling_count_does_not_multiply_document_title_evidence():
    candidates = [metadata(f"Company{index} agreement", str(index)) for index in range(30)]
    candidates.extend([metadata("Company3 agreement", "3")] * 20)
    order = explicit_title_order("Company17 agreement?", candidates)
    assert order[0] == 17
    assert len(order) == 50 and len(set(order)) == 50


def test_encoded_titles_are_matched_but_versions_and_scopes_remain_distinct():
    candidates = [metadata(f"Company{index} agreement", str(index)) for index in range(30)]
    candidates[17] = metadata("Company%31%37 agreement", "17")
    assert explicit_title_order("Company17 agreement?", candidates)[0] == 17
    candidates.append(
        metadata("Company17 agreement", "17", collection="another-authorized-library")
    )
    assert explicit_title_order("Company17 agreement?", candidates) == list(range(31))


def test_legacy_and_untrusted_metadata_cannot_create_new_results():
    candidates = [{}, {"search_title": "Company17"}, metadata("bad%FF title", "x")]
    assert explicit_title_order("Company17", candidates) == [0, 1, 2]


def test_chinese_distinctive_title_is_supported_without_changing_source_content():
    candidates = [metadata(f"普通资料编号{index}", str(index)) for index in range(30)]
    candidates[7] = metadata("星河设备维护手册", "7")
    assert explicit_title_order("星河设备的维护要求是什么?", candidates)[0] == 7


async def test_pipeline_routes_before_final_cap_and_keeps_the_real_source_score(monkeypatch):
    from app.config import settings
    from app.models import RetrievalResult, SearchRequest
    from app.services import pipeline

    metadatas = [metadata(f"Company{index} agreement", str(index)) for index in range(30)]
    documents = [f"original child {index}" for index in range(30)]
    scores = [0.9 - index / 100 for index in range(30)]
    monkeypatch.setattr(settings, "enable_reranking", True)
    monkeypatch.setattr(settings, "enable_title_routing", True)
    monkeypatch.setattr(settings, "rerank_dedup_before_limit", False)
    monkeypatch.setattr(settings, "rerank_top_k", 3)
    monkeypatch.setattr(
        pipeline, "embed_dense_and_sparse", AsyncMock(return_value=([[0.1]], [None], False))
    )
    monkeypatch.setattr(
        pipeline.qdrant,
        "vector_search",
        AsyncMock(
            return_value=RetrievalResult(texts=documents, metadatas=metadatas, distances=scores)
        ),
    )
    rank = AsyncMock(return_value=(documents, metadatas, scores))
    monkeypatch.setattr(pipeline, "rerank", rank)
    result = await pipeline.linear_search(
        SearchRequest(queries=["Company17 agreement?"], collection_names=["authorized"], k=1)
    )
    assert rank.call_args.args[3] == 30
    assert result.documents == [["original child 17"]]
    assert result.metadatas == [[metadatas[17]]]
    assert result.distances == [[scores[17]]]
