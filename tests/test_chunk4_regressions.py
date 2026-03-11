from __future__ import annotations

from services import policy_service
from services.rag_chunk_lab_service import hierarchical_chunk
from services.retrieval_quality import rerank_with_cross_encoder
from utils.config import settings


def _override_setting(key: str, value):
    original = getattr(settings, key)
    object.__setattr__(settings, key, value)
    return original


def test_merged_article_clauses_preserve_source_article():
    text = """
제8장 시간·금액·거래처·업종 공통 제약
제38조 (시간대 제약)
① 심야 시간대 지출은 검토 대상이다.
② 23시 이후 식대는 사전승인이 필요하다.
제39조 (주말·공휴일 제약)
① 주말·공휴일 지출은 원칙적으로 제한한다.
② 예외는 당직 승인 시에만 허용한다.
"""
    nodes = hierarchical_chunk(text)
    art38 = next((n for n in nodes if n.node_type == "ARTICLE" and n.regulation_article == "제38조"), None)
    assert art38 is not None
    child_articles = [c.regulation_article for c in art38.children]
    assert "제38조" in child_articles
    assert "제39조" in child_articles


def test_holiday_dense_query_includes_night_hint():
    query = policy_service._build_dense_query(  # noqa: SLF001
        {
            "case_type": "HOLIDAY_USAGE",
            "merchantName": "POC 심야 식대",
            "amount": 120000,
            "isHoliday": True,
            "occurredAt": "2026-03-07T23:30:00",
        }
    )
    assert "심야" in query


def test_semantic_group_mapping_matches_rulebook():
    mapping = policy_service._get_semantic_group_filter({"case_type": "HOLIDAY_USAGE"})  # noqa: SLF001
    assert mapping is not None
    assert "제7장" in mapping
    assert "제8장" in mapping


def test_search_policy_chunks_passes_use_hyde(monkeypatch):
    called = {"use_hyde": None}

    monkeypatch.setattr(policy_service, "_search_bm25_with_group_filter", lambda *a, **k: [])
    monkeypatch.setattr(policy_service, "_search_bm25", lambda *a, **k: [])
    monkeypatch.setattr(policy_service, "_search_lexical_legacy", lambda *a, **k: [])

    def _fake_dense(*args, **kwargs):
        called["use_hyde"] = kwargs.get("use_hyde")
        return []

    monkeypatch.setattr(policy_service, "_search_dense", _fake_dense)

    original = _override_setting("enable_hyde_query", True)
    try:
        _ = policy_service.search_policy_chunks(None, {"case_type": "HOLIDAY_USAGE"}, limit=3)
    finally:
        object.__setattr__(settings, "enable_hyde_query", original)

    assert called["use_hyde"] is True


def test_cross_encoder_absence_marks_unavailable(monkeypatch):
    monkeypatch.setattr("services.retrieval_quality._get_cross_encoder", lambda *_a, **_k: None)
    groups = [{"chunk_id": 1, "chunk_text": "제23조 식대"}]
    out = rerank_with_cross_encoder(groups, "식대 규정")
    assert out[0].get("cross_encoder_available") is False


def test_search_policy_chunks_keeps_default_return_type(monkeypatch):
    fake_chunks = [{"article": "제14조"}]
    fake_trace = {"trace_version": "policy_search.v1"}

    monkeypatch.setattr(
        policy_service,
        "_run_search_policy_chunks_pipeline",
        lambda *_a, **_k: (fake_chunks, fake_trace),
    )

    out = policy_service.search_policy_chunks(None, {"case_type": "HOLIDAY_USAGE"}, limit=3)
    assert isinstance(out, list)
    assert out == fake_chunks


def test_search_policy_chunks_debug_returns_chunks_and_trace(monkeypatch):
    fake_chunks = [{"article": "제14조"}]
    fake_trace = {"trace_version": "policy_search.v1", "search": {"selection_stage": "fused_rrf"}}

    monkeypatch.setattr(
        policy_service,
        "_run_search_policy_chunks_pipeline",
        lambda *_a, **_k: (fake_chunks, fake_trace),
    )

    out = policy_service.search_policy_chunks(
        None,
        {"case_type": "HOLIDAY_USAGE"},
        limit=3,
        debug=True,
    )
    assert isinstance(out, dict)
    assert out.get("chunks") == fake_chunks
    assert out.get("trace") == fake_trace


def test_search_policy_chunks_debug_trace_fields_and_size(monkeypatch):
    def _mk_row(cid: int):
        return {
            "chunk_id": cid,
            "doc_id": 1,
            "regulation_article": "제39조",
            "regulation_clause": "①",
            "parent_title": "제39조 (주말·공휴일 제약)",
            "chunk_text": "휴일 및 주말 지출은 검토 대상이다.",
            "node_type": "CLAUSE",
            "bm25_score": 0.3,
            "dense_score": 0.2,
            "rrf_score": 0.1,
        }

    bm25_rows = [_mk_row(i) for i in range(1, 13)]
    dense_rows = [_mk_row(i + 100) for i in range(1, 13)]

    monkeypatch.setattr(policy_service, "_search_bm25_with_group_filter", lambda *a, **k: bm25_rows)
    monkeypatch.setattr(policy_service, "_search_bm25", lambda *a, **k: [])
    monkeypatch.setattr(policy_service, "_search_dense", lambda *a, **k: dense_rows)
    monkeypatch.setattr(policy_service, "_search_lexical_legacy", lambda *a, **k: [])
    monkeypatch.setattr(policy_service, "_enrich_with_parent_context", lambda _db, chunks: chunks)

    original_fallback = _override_setting("enable_llm_rerank_fallback", False)
    try:
        out = policy_service.search_policy_chunks(
            None,
            {
                "case_type": "HOLIDAY_USAGE",
                "occurredAt": "2026-03-14T19:45:00",
                "isHoliday": True,
            },
            limit=3,
            debug=True,
            trace_level="basic",
        )
    finally:
        object.__setattr__(settings, "enable_llm_rerank_fallback", original_fallback)

    trace = out["trace"]
    assert "structured_query" in trace["search"]
    assert "dense_query" in trace["search"]
    assert "rerank_error" in trace["search"]
    assert "rerank_errors" in trace["search"]
    assert len(trace["stages"]["bm25_candidates"]) <= 10
    assert len(trace["stages"]["dense_candidates"]) <= 10
    first_selected = trace["stages"]["selected_candidates"][0]
    assert "chunk_id" in first_selected
    assert "doc_id" in first_selected
    assert isinstance(first_selected.get("domain_match_hints"), list)
    first_chunk = out["chunks"][0]
    assert first_chunk["source_strategy"].startswith("policy_search:")
