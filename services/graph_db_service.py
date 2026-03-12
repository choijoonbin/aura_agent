from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from services.policy_ref_normalizer import normalize_policy_parent_title, policy_display_label
from utils.config import settings

logger = logging.getLogger(__name__)


def _to_iso(ts: Any) -> str:
    if isinstance(ts, str) and ts.strip():
        return ts
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _safe_upper(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s.upper() if s else None


def _extract_policy_refs(result: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    direct = result.get("policy_refs")
    if isinstance(direct, list):
        refs.extend([r for r in direct if isinstance(r, dict)])

    for tr in (result.get("tool_results") or []):
        if not isinstance(tr, dict):
            continue
        if (tr.get("tool") or tr.get("skill")) != "policy_rulebook_probe":
            continue
        facts = tr.get("facts") or {}
        prs = facts.get("policy_refs") or []
        for r in prs:
            if isinstance(r, dict):
                refs.append(r)

    # article+clause 중복 제거
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for r in refs:
        a = str(r.get("article") or "").strip()
        c = str(r.get("clause") or "").strip()
        if not a:
            continue
        dedup[(a, c)] = r
    return list(dedup.values())


def _extract_claims(result: dict[str, Any]) -> list[str]:
    claims: list[str] = []

    reason = str(result.get("reasonText") or "").strip()
    if reason:
        parts = [p.strip() for p in reason.replace("\n", " ").split(". ") if p.strip()]
        claims.extend(parts[:3])

    verifier = result.get("verifier_output") or {}
    unsupported = verifier.get("unsupported_claims") or []
    for item in unsupported:
        if isinstance(item, dict):
            t = str(item.get("claim_text") or item.get("claim") or "").strip()
            if t:
                claims.append(t)

    # 중복 제거(순서 유지)
    seen: set[str] = set()
    out: list[str] = []
    for c in claims:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out[:6]


class GraphDBService:
    def __init__(self) -> None:
        self._driver = None

    @property
    def enabled(self) -> bool:
        return bool(settings.enable_graph_db)

    def _driver_or_none(self):
        if not self.enabled:
            return None
        if self._driver is not None:
            return self._driver
        try:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            return self._driver
        except Exception as e:
            logger.warning("graphdb disabled: neo4j connection init failed: %s", e)
            return None

    def sync_analysis(
        self,
        *,
        voucher_key: str,
        case_id: str,
        run_id: str,
        body_evidence: dict[str, Any] | None,
        result_payload: dict[str, Any] | None,
    ) -> None:
        drv = self._driver_or_none()
        if drv is None:
            return
        be = body_evidence or {}
        payload = result_payload or {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        result = result or {}
        completed_at = _to_iso(payload.get("completed_at") or result.get("completed_at"))
        status = _safe_upper(result.get("status"))
        case_type = _safe_upper(result.get("case_type") or be.get("case_type") or be.get("intended_risk_type"))
        severity = _safe_upper(result.get("severity"))
        score = result.get("score")
        try:
            score_val = float(score) if score is not None else None
        except Exception:
            score_val = None

        case_props = {
            "voucher_key": voucher_key,
            "case_id": case_id,
            "status": status,
            "case_type": case_type,
            "severity": severity,
            "score": score_val,
            "merchant_name": be.get("merchantName") or be.get("merchant_name"),
            "mcc_code": be.get("mccCode") or be.get("mcc_code"),
            "hr_status": be.get("hrStatus") or be.get("hr_status"),
            "amount": float(be.get("amount")) if be.get("amount") is not None else None,
            "occurred_at": be.get("occurredAt") or be.get("occurred_at"),
            "updated_at": completed_at,
        }

        policy_refs = _extract_policy_refs(result)
        claims = _extract_claims(result)

        with drv.session(database=settings.neo4j_database) as s:
            # Case + Run upsert
            s.run(
                """
                MERGE (c:Case {voucher_key: $voucher_key})
                SET c.case_id = $case_id,
                    c.status = $status,
                    c.case_type = $case_type,
                    c.severity = $severity,
                    c.score = $score,
                    c.merchant_name = $merchant_name,
                    c.mcc_code = $mcc_code,
                    c.hr_status = $hr_status,
                    c.amount = $amount,
                    c.occurred_at = $occurred_at,
                    c.updated_at = $updated_at
                MERGE (r:Run {run_id: $run_id})
                SET r.status = $status,
                    r.completed_at = $updated_at,
                    r.case_type = $case_type,
                    r.severity = $severity,
                    r.score = $score
                MERGE (r)-[:RESULT_FOR]->(c)
                """,
                {
                    **case_props,
                    "run_id": run_id,
                },
            )

            # 기존 run 하위 링크 정리 후 재생성 (idempotent)
            s.run(
                """
                MATCH (r:Run {run_id: $run_id})-[rel:HAS_CLAIM|CITES]->()
                DELETE rel
                """,
                {"run_id": run_id},
            )

            for idx, claim_text in enumerate(claims, start=1):
                claim_id = f"{run_id}:C{idx}"
                s.run(
                    """
                    MATCH (r:Run {run_id: $run_id})
                    MERGE (cl:Claim {claim_id: $claim_id})
                    SET cl.text = $text,
                        cl.source = 'reasoning'
                    MERGE (r)-[:HAS_CLAIM {order: $ord}]->(cl)
                    """,
                    {"run_id": run_id, "claim_id": claim_id, "text": claim_text, "ord": idx},
                )

            for ref in policy_refs:
                article = str(ref.get("article") or "").strip()
                clause = str(ref.get("clause") or "").strip()
                title_raw = str(ref.get("parent_title") or ref.get("title") or "").strip()
                title = normalize_policy_parent_title(article, title_raw) or None
                if not article:
                    continue
                policy_key = f"{article}:{clause}" if clause else article
                s.run(
                    """
                    MATCH (r:Run {run_id: $run_id})
                    MERGE (p:Policy {policy_key: $policy_key})
                    SET p.article = $article,
                        p.clause = $clause,
                        p.title = $title
                    MERGE (r)-[:CITES]->(p)
                    """,
                    {
                        "run_id": run_id,
                        "policy_key": policy_key,
                        "article": article,
                        "clause": clause,
                        "title": title,
                    },
                )

            # claim -> policy 연결(단순 전수 연결; 향후 citation map 기반 정밀화 가능)
            if claims and policy_refs:
                s.run(
                    """
                    MATCH (r:Run {run_id: $run_id})-[:HAS_CLAIM]->(cl:Claim)
                    MATCH (r)-[:CITES]->(p:Policy)
                    MERGE (cl)-[:SUPPORTED_BY]->(p)
                    """,
                    {"run_id": run_id},
                )

    def get_explain_path(self, *, voucher_key: str, run_id: str | None = None) -> dict[str, Any]:
        drv = self._driver_or_none()
        if drv is None:
            return {"enabled": False, "nodes": [], "edges": [], "summary": {}}

        with drv.session(database=settings.neo4j_database) as s:
            if run_id:
                rrec = s.run(
                    """
                    MATCH (c:Case {voucher_key: $voucher_key})<-[:RESULT_FOR]-(r:Run {run_id: $run_id})
                    RETURN c, r
                    """,
                    {"voucher_key": voucher_key, "run_id": run_id},
                ).single()
            else:
                rrec = s.run(
                    """
                    MATCH (c:Case {voucher_key: $voucher_key})<-[:RESULT_FOR]-(r:Run)
                    WITH c, r ORDER BY r.completed_at DESC
                    RETURN c, collect(r)[0] as r
                    """,
                    {"voucher_key": voucher_key},
                ).single()

            if not rrec:
                return {"enabled": True, "nodes": [], "edges": [], "summary": {"voucher_key": voucher_key}}

            c = dict(rrec["c"])
            r = dict(rrec["r"])

            claims = s.run(
                """
                MATCH (rn:Run {run_id: $run_id})-[hc:HAS_CLAIM]->(cl:Claim)
                RETURN cl.claim_id as claim_id, cl.text as text, hc.order as ord
                ORDER BY hc.order ASC
                """,
                {"run_id": r.get("run_id")},
            ).data()
            policies = s.run(
                """
                MATCH (rn:Run {run_id: $run_id})-[:CITES]->(p:Policy)
                RETURN p.policy_key as policy_key, p.article as article, p.clause as clause, p.title as title
                ORDER BY p.article, p.clause
                """,
                {"run_id": r.get("run_id")},
            ).data()

            nodes: list[dict[str, Any]] = [
                {"id": c.get("voucher_key"), "type": "Case", "label": c.get("voucher_key"), "props": c},
                {"id": r.get("run_id"), "type": "Run", "label": r.get("run_id"), "props": r},
            ]
            edges: list[dict[str, Any]] = [
                {"from": r.get("run_id"), "to": c.get("voucher_key"), "type": "RESULT_FOR"},
            ]

            for cl in claims:
                cid = cl.get("claim_id")
                nodes.append({"id": cid, "type": "Claim", "label": cl.get("text"), "props": cl})
                edges.append({"from": r.get("run_id"), "to": cid, "type": "HAS_CLAIM", "order": cl.get("ord")})

            for p in policies:
                pid = p.get("policy_key")
                label = policy_display_label(p.get("article"), p.get("clause"), p.get("title"))
                nodes.append(
                    {
                        "id": pid,
                        "type": "Policy",
                        "label": label,
                        "props": p,
                    }
                )
                edges.append({"from": r.get("run_id"), "to": pid, "type": "CITES"})

            # claim -> policy
            if claims and policies:
                for cl in claims:
                    for p in policies:
                        edges.append({"from": cl.get("claim_id"), "to": p.get("policy_key"), "type": "SUPPORTED_BY"})

            return {
                "enabled": True,
                "summary": {
                    "voucher_key": voucher_key,
                    "run_id": r.get("run_id"),
                    "status": r.get("status"),
                    "case_type": r.get("case_type"),
                },
                "nodes": nodes,
                "edges": edges,
            }

    def get_related_cases(self, *, voucher_key: str, limit: int = 10) -> dict[str, Any]:
        drv = self._driver_or_none()
        if drv is None:
            return {"enabled": False, "items": []}

        with drv.session(database=settings.neo4j_database) as s:
            rows = s.run(
                """
                MATCH (c:Case {voucher_key: $voucher_key})
                MATCH (o:Case)
                WHERE o.voucher_key <> c.voucher_key
                WITH c, o,
                     CASE WHEN c.merchant_name IS NOT NULL AND c.merchant_name = o.merchant_name THEN 40 ELSE 0 END AS s1,
                     CASE WHEN c.mcc_code IS NOT NULL AND c.mcc_code = o.mcc_code THEN 25 ELSE 0 END AS s2,
                     CASE WHEN c.case_type IS NOT NULL AND c.case_type = o.case_type THEN 20 ELSE 0 END AS s3,
                     CASE WHEN c.hr_status IS NOT NULL AND c.hr_status = o.hr_status THEN 15 ELSE 0 END AS s4
                WITH c, o, (s1+s2+s3+s4) AS score,
                     [x IN [
                       CASE WHEN s1>0 THEN 'same_merchant' ELSE NULL END,
                       CASE WHEN s2>0 THEN 'same_mcc' ELSE NULL END,
                       CASE WHEN s3>0 THEN 'same_case_type' ELSE NULL END,
                       CASE WHEN s4>0 THEN 'same_hr_status' ELSE NULL END
                     ] WHERE x IS NOT NULL] AS reasons
                WHERE score > 0
                RETURN o.voucher_key AS voucher_key,
                       o.case_type AS case_type,
                       o.status AS status,
                       o.severity AS severity,
                       o.score AS score_value,
                       o.updated_at AS updated_at,
                       score AS link_score,
                       reasons
                ORDER BY link_score DESC, o.updated_at DESC
                LIMIT $limit
                """,
                {"voucher_key": voucher_key, "limit": int(limit)},
            ).data()

        return {"enabled": True, "items": rows}


_graph_service = GraphDBService()


def graph_enabled() -> bool:
    return _graph_service.enabled


def sync_analysis_graph(
    *,
    voucher_key: str,
    case_id: str,
    run_id: str,
    body_evidence: dict[str, Any] | None,
    result_payload: dict[str, Any] | None,
) -> None:
    if not _graph_service.enabled:
        return
    try:
        _graph_service.sync_analysis(
            voucher_key=voucher_key,
            case_id=case_id,
            run_id=run_id,
            body_evidence=body_evidence,
            result_payload=result_payload,
        )
    except Exception as e:
        logger.warning(
            "graph sync failed run_id=%s voucher_key=%s error=%s",
            run_id,
            voucher_key,
            e,
        )


def get_case_explain_graph(*, voucher_key: str, run_id: str | None = None) -> dict[str, Any]:
    return _graph_service.get_explain_path(voucher_key=voucher_key, run_id=run_id)


def get_related_cases_graph(*, voucher_key: str, limit: int = 10) -> dict[str, Any]:
    return _graph_service.get_related_cases(voucher_key=voucher_key, limit=limit)
