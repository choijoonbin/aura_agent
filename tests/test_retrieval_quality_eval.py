from __future__ import annotations

import json

import pytest

from services import retrieval_quality as rq


def test_gold_loader_legacy_schema_conversion():
    loader = rq.GoldDatasetLoader()
    rows = [
        {
            "id": "old-1",
            "query": "q1",
            "case_type": "HOLIDAY_USAGE",
            "expected_regulation_article": "제39조",
            "expected_regulation_clause": "①",
            "acceptable_chunk_ids": ["10", "x"],
        },
        {
            "id": "old-2",
            "query": "q2",
            "case_type": "LIMIT_EXCEED",
            "expected_articles": ["제19조", "제11조"],
        },
    ]

    out = loader.load(rows)
    assert len(out) == 2
    assert out[0]["expected_targets"][0]["article"] == "제39조"
    assert out[0]["expected_targets"][0]["clause"] == "①"
    assert out[0]["acceptable_chunk_ids"] == [10]
    assert len(out[1]["expected_targets"]) == 2


def test_relevance_evaluator_strict_loose_and_negative():
    evaluator = rq.RelevanceEvaluator()
    case = {
        "expected_targets": [{"article": "제39조", "clause": "①", "weight": 1.0}],
        "acceptable_chunk_ids": [200],
        "must_not_return_articles": ["제14조"],
    }
    results = [
        {"chunk_id": 101, "article": "제39조", "clause": ""},   # loose match
        {"chunk_id": 102, "article": "제39조", "clause": "①"},  # strict match
        {"chunk_id": 103, "article": "제14조", "clause": ""},   # negative violation
    ]

    out = evaluator.evaluate_case(case, results, k=3)
    assert out["matched"] is True
    assert out["matched_at_rank"] == 1
    assert out["strict_match"] is True
    assert out["strict_matched_at_rank"] == 2
    assert out["negative_violation"] is True
    assert out["loose_recall"] == pytest.approx(1.0)
    assert out["strict_recall"] == pytest.approx(1.0)


def test_experiment_runner_compare_query_rewrite(monkeypatch):
    def _fake_run_case(
        self,
        case,
        *,
        strategy,
        k,
        use_query_rewrite,
        use_body_evidence,
        chunking_mode,
        trace_level="basic",
    ):
        if use_query_rewrite:
            results = [{"chunk_id": 1, "article": "제39조", "clause": "①", "retrieval_score": 0.9}]
        else:
            results = [{"chunk_id": 2, "article": "제11조", "clause": "", "retrieval_score": 0.3}]
        return {
            "strategy": strategy,
            "dense_query": "query",
            "rewrite_used": use_query_rewrite,
            "body_evidence_used": use_body_evidence,
            "selection_stage": "fused_rrf",
            "results": results[:k],
            "trace": {"stages": {}},
        }

    monkeypatch.setattr(rq.RetrievalRunner, "run_case", _fake_run_case)
    runner = rq.ExperimentRunner(db=None)
    dataset = [
        {
            "id": "g1",
            "query": "주말 식대 규정",
            "case_type": "HOLIDAY_USAGE",
            "expected_targets": [{"article": "제39조", "clause": "①", "weight": 1.0}],
            "acceptable_chunk_ids": [],
            "must_not_return_articles": [],
            "priority": "P0",
            "body_evidence": {},
        }
    ]
    out = runner.compare_query_rewrite(dataset, strategy="hybrid_rrf_rerank", k=5)
    assert out["rewrite_on"]["summary_metrics"]["Recall@k"] == pytest.approx(1.0)
    assert out["rewrite_off"]["summary_metrics"]["Recall@k"] == pytest.approx(0.0)


def test_load_gold_dataset_jsonl(tmp_path):
    path = tmp_path / "gold.jsonl"
    rows = [
        {
            "id": "a",
            "query": "q1",
            "case_type": "HOLIDAY_USAGE",
            "expected_targets": [{"article": "제39조", "clause": "①", "weight": 1.0}],
            "acceptable_chunk_ids": [],
        },
        {
            "id": "b",
            "query": "q2",
            "case_type": "LIMIT_EXCEED",
            "expected_targets": [{"article": "제19조", "clause": "", "weight": 1.0}],
            "acceptable_chunk_ids": [],
        },
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    loaded = rq.load_gold_dataset(path)
    assert len(loaded) == 2
    assert loaded[0]["id"] == "a"
