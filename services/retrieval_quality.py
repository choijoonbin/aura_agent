"""
Retrieval 품질 고도화 모듈.

핵심 목표
- 운영 RAG 파이프라인과 동일한 경로로 검색
- 전략별 비교(sparse/dense/hybrid/rerank)
- gold dataset 기반 정량 평가
- strict/loose relevance 분리
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import argparse
import json
import math
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

# 1차 구현: cross-encoder. 미설치 시 입력 그대로 반환
_CROSS_ENCODER_MODEL: Any = None

# 한국어 규정 텍스트용 cross-encoder (영어 ms-marco 대체)
_KO_CROSS_ENCODER_MODEL_NAME = "Dongjin-kr/ko-reranker"

# 평가 전략
RETRIEVAL_STRATEGIES: tuple[str, ...] = (
    "sparse_only",
    "dense_only",
    "hybrid_rrf",
    "hybrid_rrf_rerank",
)

# 청킹 비교 모드
CHUNKING_MODES: tuple[str, ...] = (
    "hybrid_hierarchical",  # ARTICLE + CLAUSE + ITEM
    "article_only",
    "clause_only",
)

PRIORITY_WEIGHTS: dict[str, float] = {
    "P0": 3.0,
    "P1": 2.0,
    "P2": 1.0,
    "P3": 0.5,
}


def _get_cross_encoder(model_name: str | None = None):
    global _CROSS_ENCODER_MODEL
    if _CROSS_ENCODER_MODEL is not None:
        return _CROSS_ENCODER_MODEL
    target = model_name or _KO_CROSS_ENCODER_MODEL_NAME
    try:
        from sentence_transformers import CrossEncoder

        _CROSS_ENCODER_MODEL = CrossEncoder(target)
        return _CROSS_ENCODER_MODEL
    except Exception:
        return None


def rerank_with_cross_encoder(
    groups: list[dict[str, Any]],
    query: str,
    *,
    model_name: str | None = None,
    batch_size: int = 32,
) -> list[dict[str, Any]]:
    """
    cross-encoder rerank 적용. 한국어 ko-reranker 기본.
    sentence-transformers 미설치 시 입력 그대로 반환하되 cross_encoder_available=False 마킹.
    """
    if not groups or not query or not query.strip():
        return groups
    model = _get_cross_encoder(model_name or _KO_CROSS_ENCODER_MODEL_NAME)
    if model is None:
        for g in groups:
            g["cross_encoder_available"] = False
        return groups
    try:
        passages = [g.get("chunk_text") or " ".join(g.get("snippets") or []) or "" for g in groups]
        if not any(passages):
            return groups
        pairs = [(query.strip(), p) for p in passages]
        scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
        for i, g in enumerate(groups):
            g["cross_encoder_score"] = float(scores[i]) if i < len(scores) else 0.0
            g["cross_encoder_available"] = True
        return sorted(groups, key=lambda x: x.get("cross_encoder_score", 0), reverse=True)
    except Exception:
        return groups


def rerank_with_llm_fallback(
    groups: list[dict[str, Any]],
    query: str,
    *,
    body_evidence: dict[str, Any] | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Cross-Encoder 미설치 시 LLM 기반 경량 Rerank.
    """
    if not getattr(settings, "openai_api_key", None) or not groups:
        return groups

    try:
        from openai import AzureOpenAI, OpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            kw: dict[str, Any] = {"api_key": settings.openai_api_key}
            if base_url:
                kw["base_url"] = base_url
            client = OpenAI(**kw)

        top_groups = groups[: min(max(1, top_k), 10)]
        passages_for_llm = []
        for idx, g in enumerate(top_groups):
            article = g.get("regulation_article") or g.get("article") or ""
            parent_title = g.get("parent_title") or ""
            text_preview = (g.get("chunk_text") or "")[:150]
            passages_for_llm.append(f"{idx}: [{article} {parent_title}] {text_preview}")

        system_prompt = (
            "당신은 한국 기업 경비 규정 전문가다.\n"
            "아래 케이스 상황에 대해 규정 조항들의 관련성 순서를 JSON 배열로만 응답하라.\n"
            "형식: {\"order\": [0, 2, 1, ...]} (인덱스 번호, 관련성 높은 순)\n"
            "불필요한 설명, 마크다운 금지."
        )
        user_prompt = (
            f"케이스: {query}\n\n"
            f"규정 조항 목록:\n" + "\n".join(passages_for_llm)
            + "\n\n관련성 높은 순서 (JSON 객체 order 키에 배열):"
        )

        response = client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                max_tokens=100,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            ),
        )

        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        order: list[int] = (
            parsed.get("order", [])
            if isinstance(parsed, dict)
            else (parsed if isinstance(parsed, list) else [])
        )
        order = [int(i) for i in order if isinstance(i, (int, float)) and 0 <= int(i) < len(top_groups)]
        if not order:
            return groups

        reranked = []
        seen: set[int] = set()
        for idx in order:
            if idx in seen:
                continue
            item = dict(top_groups[idx])
            item["llm_rerank_score"] = len(order) - order.index(idx)
            reranked.append(item)
            seen.add(idx)
        for idx, item in enumerate(top_groups):
            if idx not in seen:
                reranked.append(dict(item))
        reranked.extend(groups[len(top_groups):])
        return reranked
    except Exception:
        return groups


def verify_evidence_coverage(
    sentences: list[dict[str, Any]],
    retrieved_chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """evidence_verification 모듈 위임."""
    from services.evidence_verification import verify_evidence_coverage as _verify

    return _verify(sentences, retrieved_chunks)


def _normalize_token(value: Any) -> str:
    return "".join(str(value or "").split()).upper()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _extract_chunk_id(item: dict[str, Any]) -> int | None:
    raw = item.get("chunk_id")
    if raw is None:
        ids = item.get("chunk_ids") or []
        raw = ids[0] if ids else None
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def _extract_article(item: dict[str, Any]) -> str:
    return str(item.get("article") or item.get("regulation_article") or "")


def _extract_clause(item: dict[str, Any]) -> str:
    return str(item.get("clause") or item.get("regulation_clause") or "")


def _extract_score(item: dict[str, Any]) -> float:
    val = item.get("retrieval_score")
    if isinstance(val, (int, float)):
        return float(val)
    detail = item.get("score_detail") or {}
    for key in ("cross_encoder_score", "rrf_score", "bm25_score", "dense_score", "llm_rerank_score"):
        v = detail.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


@dataclass
class GoldTarget:
    article: str
    clause: str = ""
    weight: float = 1.0


class GoldDatasetLoader:
    """gold dataset 로드/정규화. legacy 필드도 지원."""

    def load(self, dataset: list[dict[str, Any]] | str | Path) -> list[dict[str, Any]]:
        if isinstance(dataset, (str, Path)):
            rows = self._load_from_path(Path(dataset))
        else:
            rows = dataset
        return [self.normalize_case(row) for row in rows]

    def _load_from_path(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"Dataset file not found: {path}")
        if path.suffix.lower() == ".jsonl":
            out: list[dict[str, Any]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                row = line.strip()
                if not row:
                    continue
                parsed = json.loads(row)
                if not isinstance(parsed, dict):
                    raise ValueError("Each JSONL row must be an object")
                out.append(parsed)
            return out
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(v, dict) for v in raw):
            raise ValueError("JSON dataset must be list[object]")
        return raw

    def normalize_case(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out.setdefault("id", out.get("case_id") or out.get("voucher_key") or "")
        out.setdefault("query", "")
        out.setdefault("case_type", "")
        out.setdefault("priority", "P2")
        out.setdefault("requires_body_evidence", True)
        out.setdefault("must_not_return_articles", [])
        out.setdefault("gold_rationale", "")
        out.setdefault("body_evidence", {})

        acceptable_chunk_ids = []
        for val in _as_list(out.get("acceptable_chunk_ids")):
            try:
                acceptable_chunk_ids.append(int(val))
            except Exception:
                continue
        out["acceptable_chunk_ids"] = acceptable_chunk_ids

        expected_targets = out.get("expected_targets")
        if not isinstance(expected_targets, list) or not expected_targets:
            expected_targets = self._convert_legacy_targets(out)

        normalized_targets: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw_t in expected_targets:
            if not isinstance(raw_t, dict):
                continue
            article = str(raw_t.get("article") or "").strip()
            clause = str(raw_t.get("clause") or "").strip()
            if not article:
                continue
            key = (_normalize_token(article), _normalize_token(clause))
            if key in seen:
                continue
            seen.add(key)
            try:
                weight = float(raw_t.get("weight", 1.0))
            except Exception:
                weight = 1.0
            normalized_targets.append(
                {
                    "article": article,
                    "clause": clause,
                    "weight": weight if weight > 0 else 1.0,
                }
            )

        if not normalized_targets:
            normalized_targets = [{"article": "제14조", "clause": "", "weight": 1.0}]

        out["expected_targets"] = normalized_targets
        out["must_not_return_articles"] = [str(v) for v in _as_list(out.get("must_not_return_articles")) if str(v).strip()]
        if not isinstance(out.get("body_evidence"), dict):
            out["body_evidence"] = {}
        return out

    def _convert_legacy_targets(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        article = str(row.get("expected_regulation_article") or row.get("expected_article") or "").strip()
        clause = str(row.get("expected_regulation_clause") or row.get("expected_clause") or "").strip()
        if article:
            targets.append({"article": article, "clause": clause, "weight": 1.0})

        for art in _as_list(row.get("expected_articles")):
            token = str(art or "").strip()
            if token:
                targets.append({"article": token, "clause": "", "weight": 1.0})

        return targets


class RetrievalRunner:
    """운영 policy_service 함수 재사용으로 평가용 retrieval 실행."""

    def __init__(self, db: Session):
        self.db = db

    def run_case(
        self,
        case: dict[str, Any],
        *,
        strategy: str,
        k: int,
        use_query_rewrite: bool,
        use_body_evidence: bool,
        chunking_mode: str,
        trace_level: str = "basic",
    ) -> dict[str, Any]:
        if strategy not in RETRIEVAL_STRATEGIES:
            raise ValueError(f"Unsupported strategy: {strategy}")
        if chunking_mode not in CHUNKING_MODES:
            raise ValueError(f"Unsupported chunking_mode: {chunking_mode}")

        from services import policy_service

        body = self._build_body_evidence(case, use_body_evidence=use_body_evidence)
        query = str(case.get("query") or "").strip()
        candidate_limit = max(k * 6, 20)
        effective_date = self._resolve_effective_date(body)
        bm25_weight, dense_weight = policy_service._get_rrf_weights(body)
        group_filter = policy_service._get_semantic_group_filter(body)
        rewrite_ctx = policy_service._rewrite_query(
            body,
            limit=k,
            effective_date=effective_date,
            candidate_limit=candidate_limit,
            group_filter=group_filter,
            bm25_weight=bm25_weight,
            dense_weight=dense_weight,
        )
        dense_query = rewrite_ctx["dense_query"] if use_query_rewrite else (query or rewrite_ctx["dense_query"])

        # 운영 기본 경로와 완전 동일하게 타는 fast-path
        if (
            strategy == "hybrid_rrf_rerank"
            and use_query_rewrite
            and use_body_evidence
            and chunking_mode == "hybrid_hierarchical"
        ):
            payload = policy_service.search_policy_chunks_with_trace(
                self.db,
                body,
                limit=k,
                trace_level=trace_level,
            )
            results = payload.get("chunks") or []
            trace = payload.get("trace") or {}
            return {
                "strategy": strategy,
                "chunking_mode": chunking_mode,
                "rewrite_used": use_query_rewrite,
                "body_evidence_used": use_body_evidence,
                "dense_query": dense_query,
                "selection_stage": trace.get("search", {}).get("selection_stage"),
                "fallback_used": trace.get("search", {}).get("fallback_used"),
                "reranker_used": trace.get("search", {}).get("reranker_used"),
                "results": results,
                "trace": trace,
            }

        bm25_results = self._bm25_candidates(
            body,
            candidate_limit=candidate_limit,
            effective_date=effective_date,
            group_filter=group_filter,
            enabled=(strategy in {"sparse_only", "hybrid_rrf", "hybrid_rrf_rerank"}),
        )
        dense_results = self._dense_candidates(
            body,
            candidate_limit=candidate_limit,
            effective_date=effective_date,
            group_filter=group_filter,
            enabled=(strategy in {"dense_only", "hybrid_rrf", "hybrid_rrf_rerank"}),
            dense_query=dense_query,
        )

        bm25_results = self._apply_chunking_mode(bm25_results, chunking_mode)
        dense_results = self._apply_chunking_mode(dense_results, chunking_mode)

        selection_stage = "empty"
        fallback_used = False
        fallback_reason: str | None = None
        reranker_used = False
        reranker_type = "none"

        if strategy == "sparse_only":
            fused = sorted(bm25_results, key=lambda x: x.get("bm25_score", 0), reverse=True)
            fused = policy_service._enrich_with_parent_context(self.db, fused[:candidate_limit])
            selection_stage = "bm25_only"
        elif strategy == "dense_only":
            fused = sorted(dense_results, key=lambda x: x.get("dense_score", 0), reverse=True)
            fused = policy_service._enrich_with_parent_context(self.db, fused[:candidate_limit])
            selection_stage = "dense_only"
        else:
            fuse_out = policy_service._fuse_candidates(
                self.db,
                body,
                bm25_results=bm25_results,
                dense_results=dense_results,
                candidate_limit=candidate_limit,
                effective_date=effective_date,
                bm25_weight=bm25_weight,
                dense_weight=dense_weight,
            )
            fused = fuse_out["enriched"]
            selection_stage = str(fuse_out["selection_stage"])
            fallback_used = bool(fuse_out["fallback_used"])
            fallback_reason = fuse_out.get("fallback_reason")

            # chunking mode는 fuse 이후에도 유지
            fused = self._apply_chunking_mode(fused, chunking_mode)

            if strategy == "hybrid_rrf_rerank":
                rerank_out = policy_service._rerank_candidates(
                    fused,
                    rerank_query=dense_query,
                    selection_stage=selection_stage,
                    body_evidence=body,
                )
                fused = rerank_out["enriched"]
                selection_stage = str(rerank_out["selection_stage"])
                reranker_used = bool(rerank_out["reranker_used"])
                reranker_type = str(rerank_out["reranker_type"] or "none")

        results = policy_service._finalize_context(
            fused,
            limit=k,
            selection_stage=selection_stage,
            reranker_used=reranker_used,
            reranker_type=reranker_type,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            body_evidence=body,
        )

        trace = {
            "trace_version": "retrieval_runner.v1",
            "search": {
                "structured_query": rewrite_ctx.get("structured_query"),
                "dense_query": dense_query,
                "selection_stage": selection_stage,
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
                "reranker_used": reranker_used,
                "reranker_type": reranker_type,
            },
            "stages": {
                "bm25_candidates": bm25_results[:10],
                "dense_candidates": dense_results[:10],
                "final_results": results,
            },
        }
        return {
            "strategy": strategy,
            "chunking_mode": chunking_mode,
            "rewrite_used": use_query_rewrite,
            "body_evidence_used": use_body_evidence,
            "dense_query": dense_query,
            "selection_stage": selection_stage,
            "fallback_used": fallback_used,
            "reranker_used": reranker_used,
            "results": results,
            "trace": trace,
        }

    def _build_body_evidence(self, case: dict[str, Any], *, use_body_evidence: bool) -> dict[str, Any]:
        case_type = str(case.get("case_type") or "").strip()
        query = str(case.get("query") or "").strip()

        if use_body_evidence:
            body = dict(case.get("body_evidence") or {})
        else:
            body = {}

        if case_type and not body.get("case_type"):
            body["case_type"] = case_type

        extras = list(body.get("_extra_keywords") or [])
        if query and query not in extras:
            extras.append(query)
        if extras:
            body["_extra_keywords"] = extras
        return body

    def _resolve_effective_date(self, body: dict[str, Any]) -> date | None:
        occurred_at = body.get("occurredAt")
        if not occurred_at:
            return None
        try:
            return date.fromisoformat(str(occurred_at)[:10])
        except Exception:
            return None

    def _bm25_candidates(
        self,
        body: dict[str, Any],
        *,
        candidate_limit: int,
        effective_date: date | None,
        group_filter: list[str] | None,
        enabled: bool,
    ) -> list[dict[str, Any]]:
        if not enabled:
            return []
        from services import policy_service

        rows = policy_service._search_bm25_with_group_filter(
            self.db,
            body,
            limit=candidate_limit,
            effective_date=effective_date,
            group_filter=group_filter,
        )
        if group_filter and len(rows) < candidate_limit:
            fallback = policy_service._search_bm25(
                self.db,
                body,
                limit=candidate_limit,
                effective_date=effective_date,
            )
            seen = {r.get("chunk_id") for r in rows}
            for r in fallback:
                cid = r.get("chunk_id")
                if cid not in seen:
                    rows.append(r)
                    seen.add(cid)
        return rows

    def _dense_candidates(
        self,
        body: dict[str, Any],
        *,
        candidate_limit: int,
        effective_date: date | None,
        group_filter: list[str] | None,
        enabled: bool,
        dense_query: str,
    ) -> list[dict[str, Any]]:
        if not enabled:
            return []
        from services import policy_service

        rows = policy_service._search_dense(
            self.db,
            body,
            limit=candidate_limit,
            effective_date=effective_date,
            group_filter=group_filter,
            use_hyde=False,
            query_text_override=dense_query,
        )
        if group_filter and len(rows) < candidate_limit:
            fallback = policy_service._search_dense(
                self.db,
                body,
                limit=candidate_limit,
                effective_date=effective_date,
                group_filter=None,
                use_hyde=False,
                query_text_override=dense_query,
            )
            seen = {r.get("chunk_id") for r in rows}
            for r in fallback:
                cid = r.get("chunk_id")
                if cid not in seen:
                    rows.append(r)
                    seen.add(cid)
        return rows

    def _apply_chunking_mode(
        self,
        rows: list[dict[str, Any]],
        chunking_mode: str,
    ) -> list[dict[str, Any]]:
        if chunking_mode == "hybrid_hierarchical":
            return rows
        if chunking_mode == "article_only":
            return [r for r in rows if str(r.get("node_type") or "").upper() == "ARTICLE"]
        if chunking_mode == "clause_only":
            return [r for r in rows if str(r.get("node_type") or "").upper() == "CLAUSE"]
        raise ValueError(f"Unsupported chunking_mode: {chunking_mode}")


class RelevanceEvaluator:
    """strict/loose relevance 판정."""

    def evaluate_case(self, case: dict[str, Any], results: list[dict[str, Any]], *, k: int) -> dict[str, Any]:
        targets = self._targets(case)
        acceptable_chunk_ids = {int(v) for v in case.get("acceptable_chunk_ids") or []}
        must_not_return = {_normalize_token(v) for v in case.get("must_not_return_articles") or []}

        loose_expected = {(_normalize_token(t.article), "") for t in targets}
        strict_expected_pairs = {
            (_normalize_token(t.article), _normalize_token(t.clause))
            for t in targets
            if _normalize_token(t.clause)
        }
        # `acceptable_chunk_ids` are treated as strict alternatives only when
        # explicit strict targets(article+clause)가 없는 케이스에서 사용한다.
        # (둘 다 있을 때 chunk_id를 strict 분모에 더하면 recall이 과도하게 페널티됨)
        use_chunk_ids_as_strict_targets = (not strict_expected_pairs) and bool(acceptable_chunk_ids)
        strict_expected = set(strict_expected_pairs)
        if use_chunk_ids_as_strict_targets:
            for cid in acceptable_chunk_ids:
                strict_expected.add((f"CHUNK:{cid}", ""))

        loose_hits: set[tuple[str, str]] = set()
        strict_hits: set[tuple[str, str]] = set()
        loose_first_rank = 0
        strict_first_rank = 0

        loose_gains: list[float] = []
        strict_gains: list[float] = []

        top_results = []
        retrieved_articles: list[str] = []
        retrieved_chunk_ids: list[int] = []

        for idx, item in enumerate(results[:k], start=1):
            chunk_id = _extract_chunk_id(item)
            article = _normalize_token(_extract_article(item))
            clause = _normalize_token(_extract_clause(item))
            retrieved_articles.append(_extract_article(item))
            if chunk_id is not None:
                retrieved_chunk_ids.append(chunk_id)

            strict_hit, loose_hit, strict_gain, loose_gain = self._judge_item(
                item,
                targets,
                acceptable_chunk_ids,
                use_chunk_ids_as_strict_targets=use_chunk_ids_as_strict_targets,
            )

            if strict_hit:
                if use_chunk_ids_as_strict_targets and chunk_id in acceptable_chunk_ids and chunk_id is not None:
                    strict_hits.add((f"CHUNK:{chunk_id}", ""))
                if article and clause and ((article, clause) in strict_expected_pairs):
                    strict_hits.add((article, clause))
                if strict_first_rank == 0:
                    strict_first_rank = idx
            if loose_hit:
                if article:
                    loose_hits.add((article, ""))
                if loose_first_rank == 0:
                    loose_first_rank = idx

            strict_gains.append(strict_gain)
            loose_gains.append(loose_gain)

            if idx <= 3:
                top_results.append(
                    {
                        "rank": idx,
                        "chunk_id": chunk_id,
                        "article": _extract_article(item),
                        "clause": _extract_clause(item),
                        "score": _extract_score(item),
                    }
                )

        loose_recall = len(loose_hits) / max(1, len(loose_expected))
        strict_recall = len(strict_hits) / max(1, len(strict_expected) if strict_expected else 1)

        loose_mrr = (1.0 / loose_first_rank) if loose_first_rank > 0 else 0.0
        strict_mrr = (1.0 / strict_first_rank) if strict_first_rank > 0 else 0.0

        loose_ndcg = self._ndcg(loose_gains, self._ideal_gains(targets, strict=False, k=k))
        strict_ndcg = self._ndcg(
            strict_gains,
            self._ideal_gains(
                targets,
                strict=True,
                k=k,
                acceptable_chunk_ids=(acceptable_chunk_ids if use_chunk_ids_as_strict_targets else None),
            ),
        )

        negative_violation = any(_normalize_token(v) in must_not_return for v in retrieved_articles)

        return {
            "matched": loose_first_rank > 0,
            "matched_at_rank": loose_first_rank if loose_first_rank > 0 else None,
            "strict_match": strict_first_rank > 0,
            "strict_matched_at_rank": strict_first_rank if strict_first_rank > 0 else None,
            "loose_recall": round(loose_recall, 6),
            "strict_recall": round(strict_recall, 6),
            "loose_mrr": round(loose_mrr, 6),
            "strict_mrr": round(strict_mrr, 6),
            "loose_ndcg": round(loose_ndcg, 6),
            "strict_ndcg": round(strict_ndcg, 6),
            "top_results": top_results,
            "retrieved_articles": retrieved_articles,
            "retrieved_chunk_ids": retrieved_chunk_ids,
            "expected_targets": [t.__dict__ for t in targets],
            "acceptable_chunk_ids": sorted(acceptable_chunk_ids),
            "negative_violation": negative_violation,
            "must_not_return_articles": [v for v in case.get("must_not_return_articles") or []],
        }

    def _targets(self, case: dict[str, Any]) -> list[GoldTarget]:
        out: list[GoldTarget] = []
        for raw in case.get("expected_targets") or []:
            if not isinstance(raw, dict):
                continue
            article = str(raw.get("article") or "").strip()
            if not article:
                continue
            clause = str(raw.get("clause") or "").strip()
            try:
                weight = float(raw.get("weight", 1.0))
            except Exception:
                weight = 1.0
            out.append(GoldTarget(article=article, clause=clause, weight=(weight if weight > 0 else 1.0)))
        return out

    def _judge_item(
        self,
        item: dict[str, Any],
        targets: list[GoldTarget],
        acceptable_chunk_ids: set[int],
        *,
        use_chunk_ids_as_strict_targets: bool,
    ) -> tuple[bool, bool, float, float]:
        chunk_id = _extract_chunk_id(item)
        article = _normalize_token(_extract_article(item))
        clause = _normalize_token(_extract_clause(item))

        strict_hit = False
        loose_hit = False
        strict_gain = 0.0
        loose_gain = 0.0

        if chunk_id is not None and chunk_id in acceptable_chunk_ids:
            strict_hit = True
            if use_chunk_ids_as_strict_targets:
                strict_gain = max(strict_gain, 2.0)

        for t in targets:
            t_article = _normalize_token(t.article)
            t_clause = _normalize_token(t.clause)
            if article and article == t_article:
                loose_hit = True
                loose_gain = max(loose_gain, float(t.weight))
                if t_clause and clause and clause == t_clause:
                    strict_hit = True
                    strict_gain = max(strict_gain, float(t.weight))

        return strict_hit, loose_hit, strict_gain, loose_gain

    def _ideal_gains(
        self,
        targets: list[GoldTarget],
        *,
        strict: bool,
        k: int,
        acceptable_chunk_ids: set[int] | None = None,
    ) -> list[float]:
        gains = [float(t.weight) for t in targets]
        if strict and acceptable_chunk_ids:
            gains.extend([2.0 for _ in acceptable_chunk_ids])
        gains.sort(reverse=True)
        return gains[:k]

    def _ndcg(self, gains: list[float], ideal_gains: list[float]) -> float:
        def _dcg(vals: list[float]) -> float:
            score = 0.0
            for i, g in enumerate(vals, start=1):
                if g <= 0:
                    continue
                score += g / math.log2(i + 1)
            return score

        dcg = _dcg(gains)
        idcg = _dcg(ideal_gains)
        return (dcg / idcg) if idcg > 0 else 0.0


class MetricCalculator:
    """dataset 전체 metric 집계."""

    def summarize(self, case_rows: list[dict[str, Any]], *, k: int) -> dict[str, Any]:
        if not case_rows:
            return {
                "Recall@k": 0.0,
                "MRR": 0.0,
                "nDCG@k": 0.0,
                "Strict Recall@k": 0.0,
                "Strict MRR": 0.0,
                "Strict nDCG@k": 0.0,
            }

        n = len(case_rows)
        summary = {
            "Recall@k": round(sum(v["loose_recall"] for v in case_rows) / n, 6),
            "MRR": round(sum(v["loose_mrr"] for v in case_rows) / n, 6),
            "nDCG@k": round(sum(v["loose_ndcg"] for v in case_rows) / n, 6),
            "Strict Recall@k": round(sum(v["strict_recall"] for v in case_rows) / n, 6),
            "Strict MRR": round(sum(v["strict_mrr"] for v in case_rows) / n, 6),
            "Strict nDCG@k": round(sum(v["strict_ndcg"] for v in case_rows) / n, 6),
            "negative_violation_rate": round(sum(1 for v in case_rows if v.get("negative_violation")) / n, 6),
            "case_count": n,
        }

        weighted = self._weighted(case_rows)
        return {"summary_metrics": summary, "weighted_metrics": weighted}

    def _priority_weight(self, priority: Any) -> float:
        token = str(priority or "P2").strip().upper()
        return PRIORITY_WEIGHTS.get(token, 1.0)

    def _weighted(self, case_rows: list[dict[str, Any]]) -> dict[str, float]:
        denom = sum(self._priority_weight(v.get("priority")) for v in case_rows) or 1.0

        def _wavg(key: str) -> float:
            return round(
                sum(float(v.get(key, 0.0)) * self._priority_weight(v.get("priority")) for v in case_rows) / denom,
                6,
            )

        return {
            "Recall@k_weighted": _wavg("loose_recall"),
            "MRR_weighted": _wavg("loose_mrr"),
            "nDCG@k_weighted": _wavg("loose_ndcg"),
            "Strict Recall@k_weighted": _wavg("strict_recall"),
            "Strict MRR_weighted": _wavg("strict_mrr"),
            "Strict nDCG@k_weighted": _wavg("strict_ndcg"),
        }


class ExperimentRunner:
    def __init__(self, db: Session):
        self.db = db
        self.loader = GoldDatasetLoader()
        self.retrieval_runner = RetrievalRunner(db)
        self.relevance = RelevanceEvaluator()
        self.metrics = MetricCalculator()

    def evaluate_gold_dataset(
        self,
        gold_dataset: list[dict[str, Any]] | str | Path,
        *,
        strategy: str,
        k: int = 5,
        use_query_rewrite: bool = True,
        use_body_evidence: bool = True,
        chunking_mode: str = "hybrid_hierarchical",
        trace_level: str = "basic",
    ) -> dict[str, Any]:
        cases = self.loader.load(gold_dataset)
        case_rows: list[dict[str, Any]] = []

        for case in cases:
            run = self.retrieval_runner.run_case(
                case,
                strategy=strategy,
                k=k,
                use_query_rewrite=use_query_rewrite,
                use_body_evidence=use_body_evidence,
                chunking_mode=chunking_mode,
                trace_level=trace_level,
            )
            case_eval = self.relevance.evaluate_case(case, run.get("results") or [], k=k)
            row = {
                "id": case.get("id"),
                "query": case.get("query"),
                "priority": case.get("priority"),
                "matched": case_eval["matched"],
                "matched_at_rank": case_eval["matched_at_rank"],
                "strict_match": case_eval["strict_match"],
                "strict_matched_at_rank": case_eval["strict_matched_at_rank"],
                "top_results": case_eval["top_results"],
                "retrieval_strategy": strategy,
                "dense_query": run.get("dense_query"),
                "rewrite_used": run.get("rewrite_used"),
                "body_evidence_used": run.get("body_evidence_used"),
                "retrieved_articles": case_eval["retrieved_articles"],
                "retrieved_chunk_ids": case_eval["retrieved_chunk_ids"],
                "expected_targets": case_eval["expected_targets"],
                "acceptable_chunk_ids": case_eval["acceptable_chunk_ids"],
                "negative_violation": case_eval["negative_violation"],
                "must_not_return_articles": case_eval["must_not_return_articles"],
                "selection_stage": run.get("selection_stage"),
                "trace": run.get("trace"),
                "loose_recall": case_eval["loose_recall"],
                "strict_recall": case_eval["strict_recall"],
                "loose_mrr": case_eval["loose_mrr"],
                "strict_mrr": case_eval["strict_mrr"],
                "loose_ndcg": case_eval["loose_ndcg"],
                "strict_ndcg": case_eval["strict_ndcg"],
            }
            case_rows.append(row)

        summary = self.metrics.summarize(case_rows, k=k)
        return {
            "strategy": strategy,
            "k": k,
            "rewrite_used": use_query_rewrite,
            "body_evidence_used": use_body_evidence,
            "chunking_mode": chunking_mode,
            **summary,
            "cases": case_rows,
        }

    def compare_retrieval_strategies(
        self,
        gold_dataset: list[dict[str, Any]] | str | Path,
        *,
        k: int = 5,
        use_query_rewrite: bool = True,
        use_body_evidence: bool = True,
        chunking_mode: str = "hybrid_hierarchical",
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for strategy in RETRIEVAL_STRATEGIES:
            out[strategy] = self.evaluate_gold_dataset(
                gold_dataset,
                strategy=strategy,
                k=k,
                use_query_rewrite=use_query_rewrite,
                use_body_evidence=use_body_evidence,
                chunking_mode=chunking_mode,
            )
        return out

    def compare_query_rewrite(
        self,
        gold_dataset: list[dict[str, Any]] | str | Path,
        *,
        strategy: str = "hybrid_rrf_rerank",
        k: int = 5,
        use_body_evidence: bool = True,
        chunking_mode: str = "hybrid_hierarchical",
    ) -> dict[str, Any]:
        return {
            "rewrite_on": self.evaluate_gold_dataset(
                gold_dataset,
                strategy=strategy,
                k=k,
                use_query_rewrite=True,
                use_body_evidence=use_body_evidence,
                chunking_mode=chunking_mode,
            ),
            "rewrite_off": self.evaluate_gold_dataset(
                gold_dataset,
                strategy=strategy,
                k=k,
                use_query_rewrite=False,
                use_body_evidence=use_body_evidence,
                chunking_mode=chunking_mode,
            ),
        }

    def compare_chunking_modes(
        self,
        gold_dataset: list[dict[str, Any]] | str | Path,
        *,
        strategy: str = "hybrid_rrf_rerank",
        k: int = 5,
        use_query_rewrite: bool = True,
        use_body_evidence: bool = True,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mode in CHUNKING_MODES:
            out[mode] = self.evaluate_gold_dataset(
                gold_dataset,
                strategy=strategy,
                k=k,
                use_query_rewrite=use_query_rewrite,
                use_body_evidence=use_body_evidence,
                chunking_mode=mode,
            )
        return out

    def debug_single_case(
        self,
        case_id: str,
        gold_dataset: list[dict[str, Any]] | str | Path,
        *,
        strategy: str = "hybrid_rrf_rerank",
        k: int = 5,
        use_query_rewrite: bool = True,
        use_body_evidence: bool = True,
        chunking_mode: str = "hybrid_hierarchical",
    ) -> dict[str, Any]:
        cases = self.loader.load(gold_dataset)
        target = next((c for c in cases if str(c.get("id")) == str(case_id)), None)
        if target is None:
            raise ValueError(f"Case not found: {case_id}")

        run = self.retrieval_runner.run_case(
            target,
            strategy=strategy,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
            trace_level="full",
        )
        case_eval = self.relevance.evaluate_case(target, run.get("results") or [], k=k)
        return {
            "id": target.get("id"),
            "query": target.get("query"),
            "retrieval_strategy": strategy,
            "dense_query": run.get("dense_query"),
            "rewrite_used": use_query_rewrite,
            "body_evidence_used": use_body_evidence,
            "retrieval_trace": run.get("trace"),
            "top_candidates": (run.get("trace") or {}).get("stages", {}).get("bm25_candidates", []),
            "rerank_results": (run.get("trace") or {}).get("stages", {}).get("reranked_candidates", []),
            "final_results": run.get("results") or [],
            "strict_vs_loose": {
                "loose_recall": case_eval["loose_recall"],
                "strict_recall": case_eval["strict_recall"],
                "loose_mrr": case_eval["loose_mrr"],
                "strict_mrr": case_eval["strict_mrr"],
                "loose_ndcg": case_eval["loose_ndcg"],
                "strict_ndcg": case_eval["strict_ndcg"],
                "matched_at_rank": case_eval["matched_at_rank"],
                "strict_matched_at_rank": case_eval["strict_matched_at_rank"],
            },
            "expected_targets": case_eval["expected_targets"],
            "acceptable_chunk_ids": case_eval["acceptable_chunk_ids"],
            "retrieved_articles": case_eval["retrieved_articles"],
            "retrieved_chunk_ids": case_eval["retrieved_chunk_ids"],
        }


# --- Public wrappers ---

def run_retrieval_strategy(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    strategy: str = "hybrid_rrf_rerank",
    chunking_mode: str = "hybrid_hierarchical",
    limit: int = 5,
    use_query_rewrite: bool = True,
    use_body_evidence: bool = True,
) -> dict[str, Any]:
    """Backward-compatible wrapper."""
    runner = RetrievalRunner(db)
    fake_case = {
        "id": "single",
        "query": "",
        "case_type": body_evidence.get("case_type"),
        "body_evidence": body_evidence,
        "expected_targets": [{"article": "제14조", "clause": "", "weight": 1.0}],
        "acceptable_chunk_ids": [],
        "priority": "P2",
    }
    return runner.run_case(
        fake_case,
        strategy=strategy,
        k=limit,
        use_query_rewrite=use_query_rewrite,
        use_body_evidence=use_body_evidence,
        chunking_mode=chunking_mode,
    )


def evaluate_gold_dataset(
    gold_dataset: list[dict[str, Any]] | str | Path,
    strategy: str,
    k: int = 5,
    use_query_rewrite: bool = True,
    use_body_evidence: bool = True,
    *,
    db: Session | None = None,
    chunking_mode: str = "hybrid_hierarchical",
) -> dict[str, Any]:
    """요구 스펙의 대표 평가 함수."""
    if db is not None:
        runner = ExperimentRunner(db)
        return runner.evaluate_gold_dataset(
            gold_dataset,
            strategy=strategy,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
        )

    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        runner = ExperimentRunner(session)
        return runner.evaluate_gold_dataset(
            gold_dataset,
            strategy=strategy,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
        )


def compare_retrieval_strategies(
    gold_dataset: list[dict[str, Any]] | str | Path,
    *,
    db: Session | None = None,
    k: int = 5,
    use_query_rewrite: bool = True,
    use_body_evidence: bool = True,
    chunking_mode: str = "hybrid_hierarchical",
) -> dict[str, Any]:
    if db is not None:
        return ExperimentRunner(db).compare_retrieval_strategies(
            gold_dataset,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
        )
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        return ExperimentRunner(session).compare_retrieval_strategies(
            gold_dataset,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
        )


def compare_query_rewrite(
    gold_dataset: list[dict[str, Any]] | str | Path,
    *,
    db: Session | None = None,
    strategy: str = "hybrid_rrf_rerank",
    k: int = 5,
    use_body_evidence: bool = True,
    chunking_mode: str = "hybrid_hierarchical",
) -> dict[str, Any]:
    if db is not None:
        return ExperimentRunner(db).compare_query_rewrite(
            gold_dataset,
            strategy=strategy,
            k=k,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
        )
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        return ExperimentRunner(session).compare_query_rewrite(
            gold_dataset,
            strategy=strategy,
            k=k,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
        )


def compare_chunking_modes(
    gold_dataset: list[dict[str, Any]] | str | Path,
    *,
    db: Session | None = None,
    strategy: str = "hybrid_rrf_rerank",
    k: int = 5,
    use_query_rewrite: bool = True,
    use_body_evidence: bool = True,
) -> dict[str, Any]:
    if db is not None:
        return ExperimentRunner(db).compare_chunking_modes(
            gold_dataset,
            strategy=strategy,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
        )
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        return ExperimentRunner(session).compare_chunking_modes(
            gold_dataset,
            strategy=strategy,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
        )


def debug_single_case(
    case_id: str,
    gold_dataset: list[dict[str, Any]] | str | Path,
    *,
    db: Session | None = None,
    strategy: str = "hybrid_rrf_rerank",
    k: int = 5,
    use_query_rewrite: bool = True,
    use_body_evidence: bool = True,
    chunking_mode: str = "hybrid_hierarchical",
) -> dict[str, Any]:
    if db is not None:
        return ExperimentRunner(db).debug_single_case(
            case_id,
            gold_dataset,
            strategy=strategy,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
        )
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        return ExperimentRunner(session).debug_single_case(
            case_id,
            gold_dataset,
            strategy=strategy,
            k=k,
            use_query_rewrite=use_query_rewrite,
            use_body_evidence=use_body_evidence,
            chunking_mode=chunking_mode,
        )


def load_gold_dataset(path: str | Path) -> list[dict[str, Any]]:
    return GoldDatasetLoader().load(path)


def retrieval_quality_comparison(
    body_evidence: dict[str, Any],
    result_a: list[dict[str, Any]],
    result_b: list[dict[str, Any]],
) -> dict[str, Any]:
    """하위 호환 비교 함수."""
    ids_a = {_extract_chunk_id(v) for v in result_a if _extract_chunk_id(v) is not None}
    ids_b = {_extract_chunk_id(v) for v in result_b if _extract_chunk_id(v) is not None}
    overlap = len(ids_a & ids_b)
    union = len(ids_a | ids_b)
    return {
        "strategy_a_count": len(result_a),
        "strategy_b_count": len(result_b),
        "overlap_count": overlap,
        "jaccard_topk": round((overlap / union) if union > 0 else 0.0, 6),
        "comparison_ready": True,
        "case_type": body_evidence.get("case_type"),
    }


def _default_dataset_path() -> str:
    return str(Path("docs/Edu/retrieval_gold_template.jsonl"))


def _write_or_print(payload: dict[str, Any], output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"[OK] report saved: {output}")
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG retrieval quality evaluator")
    parser.add_argument("--dataset", default=_default_dataset_path(), help="gold dataset path (.json/.jsonl)")
    parser.add_argument("--strategy", default="hybrid_rrf_rerank", choices=RETRIEVAL_STRATEGIES)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--chunking-mode", default="hybrid_hierarchical", choices=CHUNKING_MODES)
    parser.add_argument("--rewrite", dest="rewrite", action="store_true", default=True)
    parser.add_argument("--no-rewrite", dest="rewrite", action="store_false")
    parser.add_argument("--body-evidence", dest="body_evidence", action="store_true", default=True)
    parser.add_argument("--no-body-evidence", dest="body_evidence", action="store_false")
    parser.add_argument("--evaluate", action="store_true", help="run evaluate_gold_dataset")
    parser.add_argument("--compare-strategies", action="store_true", help="compare base retrieval strategies")
    parser.add_argument("--compare-rewrite", action="store_true", help="compare rewrite on/off")
    parser.add_argument("--compare-chunking", action="store_true", help="compare chunking modes")
    parser.add_argument("--debug-case", default="", help="debug single case id")
    parser.add_argument("--output", default="", help="optional output json path")
    args = parser.parse_args()

    dataset = args.dataset
    output = args.output or None

    # 실행 모드 선택
    if args.debug_case:
        payload = debug_single_case(
            args.debug_case,
            dataset,
            strategy=args.strategy,
            k=args.k,
            use_query_rewrite=args.rewrite,
            use_body_evidence=args.body_evidence,
            chunking_mode=args.chunking_mode,
        )
        _write_or_print(payload, output)
        return

    if args.compare_strategies:
        payload = compare_retrieval_strategies(
            dataset,
            k=args.k,
            use_query_rewrite=args.rewrite,
            use_body_evidence=args.body_evidence,
            chunking_mode=args.chunking_mode,
        )
        _write_or_print(payload, output)
        return

    if args.compare_rewrite:
        payload = compare_query_rewrite(
            dataset,
            strategy=args.strategy,
            k=args.k,
            use_body_evidence=args.body_evidence,
            chunking_mode=args.chunking_mode,
        )
        _write_or_print(payload, output)
        return

    if args.compare_chunking:
        payload = compare_chunking_modes(
            dataset,
            strategy=args.strategy,
            k=args.k,
            use_query_rewrite=args.rewrite,
            use_body_evidence=args.body_evidence,
        )
        _write_or_print(payload, output)
        return

    if args.evaluate or True:
        payload = evaluate_gold_dataset(
            dataset,
            strategy=args.strategy,
            k=args.k,
            use_query_rewrite=args.rewrite,
            use_body_evidence=args.body_evidence,
            chunking_mode=args.chunking_mode,
        )
        _write_or_print(payload, output)


if __name__ == "__main__":
    main()
