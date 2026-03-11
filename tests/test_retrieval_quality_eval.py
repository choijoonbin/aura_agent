from __future__ import annotations

import json

import pytest

from services import retrieval_quality as rq


def test_compute_retrieval_metrics_for_case_article_match():
    retrieved = [
        {"chunk_id": 101, "article": "제1조"},
        {"chunk_id": 102, "article": "제14조"},
        {"chunk_id": 103, "article": "제23조"},
    ]
    gold = {"expected_regulation_article": "제14조"}

    metrics = rq.compute_retrieval_metrics_for_case(
        retrieved,
        gold,
        top_ks=(3, 5),
        ndcg_k=5,
    )

    assert metrics["recall@3"] == pytest.approx(1.0)
    assert metrics["recall@5"] == pytest.approx(1.0)
    assert metrics["mrr"] == pytest.approx(0.5)
    assert metrics["ndcg@5"] == pytest.approx(1 / 1.5849625, rel=1e-3)


def test_compute_retrieval_metrics_for_case_chunk_id_recall_fraction():
    retrieved = [
        {"chunk_id": 10, "article": "제10조"},
        {"chunk_id": 99, "article": "제99조"},
        {"chunk_id": 50, "article": "제50조"},
    ]
    gold = {"acceptable_chunk_ids": [10, 11]}

    metrics = rq.compute_retrieval_metrics_for_case(
        retrieved,
        gold,
        top_ks=(3,),
        ndcg_k=3,
    )

    assert metrics["recall@3"] == pytest.approx(0.5)
    assert metrics["mrr"] == pytest.approx(1.0)


def test_evaluate_gold_dataset_aggregates(monkeypatch):
    def _fake_run(_db, body_evidence, *, strategy, chunking_mode, limit):
        keywords = body_evidence.get("_extra_keywords") or []
        query = keywords[0] if keywords else ""
        if query == "q-hit":
            results = [{"chunk_id": 1, "article": "제14조"}]
        else:
            results = [{"chunk_id": 2, "article": "제23조"}]
        return {
            "strategy": strategy,
            "chunking_mode": chunking_mode,
            "selection_stage": "fused_rrf",
            "fallback_used": False,
            "reranker_used": False,
            "results": results[:limit],
        }

    monkeypatch.setattr(rq, "run_retrieval_strategy", _fake_run)

    gold_examples = [
        {"id": "c1", "query": "q-hit", "case_type": "HOLIDAY_USAGE", "expected_regulation_article": "제14조"},
        {"id": "c2", "query": "q-miss", "case_type": "HOLIDAY_USAGE", "expected_regulation_article": "제14조"},
    ]

    report = rq.evaluate_gold_dataset(
        None,
        gold_examples,
        strategies=["sparse_only"],
        chunking_modes=["hybrid_hierarchical"],
        top_ks=(3, 5),
        ndcg_k=5,
    )

    assert report["dataset_size"] == 2
    assert len(report["reports"]) == 1
    summary = report["reports"][0]["summary"]
    assert summary["recall@3"] == pytest.approx(0.5)
    assert "mrr" in summary
    assert "ndcg@5" in summary


def test_compare_retrieval_strategies(monkeypatch):
    def _fake_run(_db, _body, *, strategy, chunking_mode, limit):
        return {
            "strategy": strategy,
            "chunking_mode": chunking_mode,
            "selection_stage": "fused_rrf",
            "fallback_used": False,
            "reranker_used": strategy.endswith("rerank"),
            "reranker_type": "cross_encoder" if strategy.endswith("rerank") else "none",
            "results": [{"chunk_id": 11, "article": "제11조"}][:limit],
        }

    monkeypatch.setattr(rq, "run_retrieval_strategy", _fake_run)
    out = rq.compare_retrieval_strategies(
        None,
        {"case_type": "HOLIDAY_USAGE"},
        strategies=["sparse_only", "hybrid_rrf_rerank"],
        limit=3,
    )

    assert out["comparison_ready"] is True
    assert out["run_count"] == 2
    assert len(out["runs"]) == 2


def test_load_gold_dataset_jsonl(tmp_path):
    path = tmp_path / "gold.jsonl"
    rows = [
        {"id": "a", "query": "q1", "expected_regulation_article": "제14조"},
        {"id": "b", "query": "q2", "expected_regulation_article": "제23조"},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    loaded = rq.load_gold_dataset(path)
    assert len(loaded) == 2
    assert loaded[0]["id"] == "a"
