from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

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


def _extract_claim_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    """verifier_output.claim_results 기반 구조화 클레임 추출.
    covered/supporting_articles/gap 정보를 보존해 정밀한 claim→policy 연결에 사용.
    claim_results가 없으면 reasonText 문장 분리로 fallback."""
    verifier = result.get("verifier_output") or {}
    raw_list = verifier.get("claim_results") or []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in raw_list:
        if not isinstance(item, dict):
            item = item.model_dump() if hasattr(item, "model_dump") else {}
        text = str(item.get("claim") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append({
            "text": text,
            "covered": bool(item.get("covered")),
            "supporting_articles": [str(a) for a in (item.get("supporting_articles") or []) if a],
            "gap": str(item.get("gap") or "").strip(),
        })

    # fallback: claim_results 없으면 reasonText 첫 3문장 (covered=True 가정)
    if not out:
        reason = str(result.get("reasonText") or "").strip()
        if reason:
            for p in [s.strip() for s in reason.replace("\n", " ").split(". ") if s.strip()][:3]:
                if p not in seen:
                    seen.add(p)
                    out.append({"text": p, "covered": True, "supporting_articles": [], "gap": ""})

    return out[:8]


def _extract_articles_from_text(text: str) -> list[str]:
    """Claim 문장에서 조문 토큰(예: 제23조, 제39조)을 추출한다."""
    raw = str(text or "")
    hits = re.findall(r"(제\s*\d+\s*조)", raw)
    out: list[str] = []
    seen: set[str] = set()
    for h in hits:
        normalized = re.sub(r"\s+", "", h)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


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
        claim_results = _extract_claim_results(result)

        # article → policy_key 역맵 (claim별 정밀 연결용)
        article_to_policy_key: dict[str, str] = {}
        for ref in policy_refs:
            a = str(ref.get("article") or "").strip()
            c = str(ref.get("clause") or "").strip()
            if a:
                article_to_policy_key[a] = f"{a}:{c}" if c else a

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

            # 기존 run 하위 링크 + claim→policy 엣지 정리 (idempotent)
            s.run(
                """
                MATCH (r:Run {run_id: $run_id})-[:HAS_CLAIM]->(cl:Claim)-[rel:SUPPORTED_BY]->()
                DELETE rel
                """,
                {"run_id": run_id},
            )
            s.run(
                """
                MATCH (r:Run {run_id: $run_id})-[rel:HAS_CLAIM|CITES]->()
                DELETE rel
                """,
                {"run_id": run_id},
            )

            # Claim 노드 저장 (covered/gap 포함)
            for idx, cr in enumerate(claim_results, start=1):
                claim_id = f"{run_id}:C{idx}"
                s.run(
                    """
                    MATCH (r:Run {run_id: $run_id})
                    MERGE (cl:Claim {claim_id: $claim_id})
                    SET cl.text = $text,
                        cl.covered = $covered,
                        cl.gap = $gap,
                        cl.source = 'verifier'
                    MERGE (r)-[:HAS_CLAIM {order: $ord}]->(cl)
                    """,
                    {
                        "run_id": run_id,
                        "claim_id": claim_id,
                        "text": cr["text"],
                        "covered": cr["covered"],
                        "gap": cr["gap"],
                        "ord": idx,
                    },
                )

            # Policy 노드 저장
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

            # claim → policy 정밀 연결 (supporting_articles 기반, covered 클레임만)
            for idx, cr in enumerate(claim_results, start=1):
                claim_id = f"{run_id}:C{idx}"
                if not cr["covered"]:
                    continue
                articles = [str(a).strip() for a in (cr.get("supporting_articles") or []) if str(a).strip()]
                if not articles:
                    # fallback: supporting_articles 누락 시 claim 텍스트에서 조문을 추출해 연결한다.
                    articles = _extract_articles_from_text(cr.get("text") or "")
                for article in articles:
                    pkey = article_to_policy_key.get(article)
                    if pkey:
                        s.run(
                            """
                            MATCH (cl:Claim {claim_id: $claim_id})
                            MATCH (p:Policy {policy_key: $pkey})
                            MERGE (cl)-[:SUPPORTED_BY]->(p)
                            """,
                            {"claim_id": claim_id, "pkey": pkey},
                        )
                        continue

                    # article은 있으나 policy_key 매핑이 없으면 같은 run의 CITES Policy(article 일치)를 우선 연결
                    row = s.run(
                        """
                        MATCH (r:Run {run_id: $run_id})-[:CITES]->(p:Policy)
                        WHERE p.article = $article
                        RETURN p.policy_key as policy_key
                        LIMIT 1
                        """,
                        {"run_id": run_id, "article": article},
                    ).single()
                    if row and row.get("policy_key"):
                        s.run(
                            """
                            MATCH (cl:Claim {claim_id: $claim_id})
                            MATCH (p:Policy {policy_key: $pkey})
                            MERGE (cl)-[:SUPPORTED_BY]->(p)
                            """,
                            {"claim_id": claim_id, "pkey": row.get("policy_key")},
                        )
                    else:
                        # policy_refs에도 없고 run 내 CITES도 없으면 최소 Policy 노드를 생성해 연결
                        s.run(
                            """
                            MATCH (r:Run {run_id: $run_id})
                            MATCH (cl:Claim {claim_id: $claim_id})
                            MERGE (p:Policy {policy_key: $article})
                            SET p.article = $article
                            MERGE (r)-[:CITES]->(p)
                            MERGE (cl)-[:SUPPORTED_BY]->(p)
                            """,
                            {"run_id": run_id, "claim_id": claim_id, "article": article},
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
                RETURN cl.claim_id as claim_id, cl.text as text,
                       cl.covered as covered, cl.gap as gap,
                       hc.order as ord
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
                covered = cl.get("covered")
                # covered가 None(기존 데이터)이면 True로 가정
                covered_flag = covered if covered is not None else True
                nodes.append({
                    "id": cid,
                    "type": "Claim",
                    "label": cl.get("text"),
                    "props": {**cl, "covered": covered_flag},
                })
                edges.append({"from": r.get("run_id"), "to": cid, "type": "HAS_CLAIM", "order": cl.get("ord")})

            for p in policies:
                pid = p.get("policy_key")
                label = policy_display_label(p.get("article"), p.get("clause"), p.get("title"))
                nodes.append({"id": pid, "type": "Policy", "label": label, "props": p})
                edges.append({"from": r.get("run_id"), "to": pid, "type": "CITES"})

            # claim → policy: Neo4j의 실제 SUPPORTED_BY 엣지 조회 (정밀 연결)
            supported_rows = s.run(
                """
                MATCH (rn:Run {run_id: $run_id})-[:HAS_CLAIM]->(cl:Claim)-[:SUPPORTED_BY]->(p:Policy)
                RETURN cl.claim_id as claim_id, p.policy_key as policy_key
                """,
                {"run_id": r.get("run_id")},
            ).data()
            for row in supported_rows:
                edges.append({"from": row["claim_id"], "to": row["policy_key"], "type": "SUPPORTED_BY"})

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

    def purge_demo_data(self, *, voucher_keys: Iterable[str], run_ids: Iterable[str]) -> dict[str, Any]:
        """시연 데이터 삭제 시 Neo4j의 Case/Run/Claim 흔적을 함께 정리한다."""
        drv = self._driver_or_none()
        if drv is None:
            return {"enabled": False, "run_ids": 0, "voucher_keys": 0}

        run_list = [str(x).strip() for x in run_ids if str(x).strip()]
        voucher_list = [str(x).strip() for x in voucher_keys if str(x).strip()]
        if not run_list and not voucher_list:
            return {"enabled": True, "run_ids": 0, "voucher_keys": 0}

        with drv.session(database=settings.neo4j_database) as s:
            if run_list:
                # Run에 매달린 Claim부터 제거 후 Run 제거
                s.run(
                    """
                    UNWIND $run_ids AS rid
                    OPTIONAL MATCH (:Run {run_id: rid})-[:HAS_CLAIM]->(cl:Claim)
                    DETACH DELETE cl
                    """,
                    {"run_ids": run_list},
                )
                s.run(
                    """
                    UNWIND $run_ids AS rid
                    OPTIONAL MATCH (r:Run {run_id: rid})
                    DETACH DELETE r
                    """,
                    {"run_ids": run_list},
                )

            if voucher_list:
                s.run(
                    """
                    UNWIND $voucher_keys AS vk
                    OPTIONAL MATCH (c:Case {voucher_key: vk})
                    DETACH DELETE c
                    """,
                    {"voucher_keys": voucher_list},
                )

            # 고아 노드 정리
            s.run("MATCH (cl:Claim) WHERE NOT (cl)--() DELETE cl")
            s.run("MATCH (p:Policy) WHERE NOT (p)--() DELETE p")

        return {"enabled": True, "run_ids": len(run_list), "voucher_keys": len(voucher_list)}


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


def purge_demo_graph_data(*, voucher_keys: Iterable[str], run_ids: Iterable[str]) -> dict[str, Any]:
    return _graph_service.purge_demo_data(voucher_keys=voucher_keys, run_ids=run_ids)
