from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from fastapi import Body, Depends, File, FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from agent.aura_bridge import run_agent_analysis
from db.session import get_db
from services.agent_studio_service import get_agent_detail, list_agents
from services.case_service import (
    build_analysis_payload,
    get_agent_case_status,
    list_vouchers,
    run_case_screening,
    update_agent_case_status_from_run,
    upsert_agent_case_from_screening_result,
)
from services.evidence_compare_service import compare_evidence_to_voucher
from services.evidence_extraction import extract_from_bytes
from services.demo_data_service import clear_demo_data, list_demo_scenarios, list_seeded_demo_cases, seed_demo_scenarios
from services.persistence_service import persist_analysis_result
from services.chunking_pipeline import run_chunking_pipeline
from services.rag_library_service import get_rag_document_detail, list_rag_documents
from services.run_diagnostics import compare_runs_diagnostics, get_run_diagnostics
from services.runtime_persistence_service import (
    create_analysis_run_row,
    get_latest_run_id_by_case,
    get_persisted_timeline,
    get_run_aux_state,
    list_run_ids_by_case,
    log_run_event,
)
from services.schemas import (
    AnalysisStartRequest,
    AnalysisStartResponse,
    HitlDraftRequest,
    HitlSubmitRequest,
    HitlSubmitResponse,
    ReviewSubmitRequest,
)
from services.stream_runtime import runtime
from utils.config import ensure_source_paths, settings

# 터미널에서 [RESUME_TRACE]/[HITL_CLOSE]/[analysis] 등 INFO 로그 확인용 (uvicorn 실행 터미널에 출력)
try:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S", force=True)
except TypeError:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S")

logger = logging.getLogger(__name__)

app = FastAPI(title="AuraAgent PoC API", version="0.3.0")


def _ensure_runtime_resume_context(run_id: str, lineage: dict[str, Any] | None) -> None:
    """
    DB(aux)에서만 lineage를 복원한 재개 요청(review-submit/hitl)에서도
    stream endpoint가 404가 되지 않도록 runtime queue/lineage를 보장한다.
    """
    info = lineage or {}
    case_id = str(info.get("case_id") or "").strip()
    if not case_id:
        return
    if runtime.get_queue(run_id) is not None and runtime.get_lineage(run_id) is not None:
        return
    runtime.ensure_run_context(
        case_id=case_id,
        run_id=run_id,
        parent_run_id=info.get("parent_run_id"),
        mode=str(info.get("mode") or "primary"),
        created_at=info.get("created_at"),
    )
    logger.info("[analysis] runtime resume context ensured run_id=%s case_id=%s", run_id, case_id)


@app.on_event("startup")
async def startup() -> None:
    ensure_source_paths()
    # LangGraph/Checkpointer를 시작 시점에 선초기화해 첫 요청 지연을 줄인다.
    # postgres checkpointer 초기화 실패/지연 시에도 서비스는 계속 기동(요청 시 lazy 재시도).
    try:
        from agent.langgraph_agent import build_agent_graph

        await asyncio.wait_for(asyncio.to_thread(build_agent_graph), timeout=20)
        logger.info("LangGraph prewarm completed")
    except Exception as exc:
        logger.warning("LangGraph prewarm skipped: %s", exc)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "env": settings.app_env,
        "tenant_id": settings.default_tenant_id,
        "user_id": settings.default_user_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "agent_runtime_mode": settings.agent_runtime_mode,
        "enable_multi_agent": settings.enable_multi_agent,
        "enable_langgraph_if_available": settings.enable_langgraph_if_available,
    }


@app.get("/api/v1/vouchers")
def get_vouchers(
    queue: str = Query("all", pattern="^(all|pending)$"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        rows = list_vouchers(db, queue=queue, limit=limit)
    except Exception as e:
        logger.exception("list_vouchers failed: %s", e)
        raise HTTPException(status_code=500, detail=f"voucher list failed: {e!s}") from e
    # 목록/KPI 초깃값이 우측 상세(run 최신 결과)와 어긋나지 않도록,
    # 최신 run 상태를 우선 반영하고 필요 시 HITL 대기만 파생한다.
    out = [r.model_dump() for r in rows]
    for item in out:
        try:
            voucher_key = item.get("voucher_key")
            if not voucher_key:
                continue
            case_id = f"POC-{voucher_key}"
            run_id = runtime.latest_run_of_case(case_id) or get_latest_run_id_by_case(db, case_id=case_id)
            if not run_id:
                continue
            aux = get_run_aux_state(db, run_id=run_id)
            hitl_req = runtime.get_hitl_request(run_id) or aux.get("hitl_request")
            hitl_res = runtime.get_hitl_response(run_id) or aux.get("hitl_response")
            result = runtime.get_result(run_id)
            if result is None and aux.get("result_payload") is not None:
                result = {"result": aux.get("result_payload")}

            derived_status = None
            if isinstance(result, dict) and isinstance(result.get("result"), dict):
                derived_status = (result.get("result") or {}).get("status")
            if not derived_status and hitl_req and not hitl_res:
                derived_status = "HITL_REQUIRED"
            if derived_status:
                item["case_status"] = str(derived_status).strip().upper()
        except Exception as e:
            logger.debug("voucher run_id/aux check failed for %s: %s", item.get("voucher_key"), e)
            continue
    return {"items": out, "total": len(out)}


@app.get("/api/v1/rag/documents")
def get_rag_documents(db: Session = Depends(get_db)) -> dict[str, Any]:
    items = list_rag_documents(db)
    return {"items": items, "total": len(items)}


@app.get("/api/v1/rag/documents/{doc_id}")
def get_rag_document(doc_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = get_rag_document_detail(db, doc_id)
    if not item:
        raise HTTPException(status_code=404, detail="rag document not found")
    return item


class RechunkRequest(BaseModel):
    raw_text: str


@app.post("/api/v1/rag/documents/{doc_id}/rechunk")
def post_rag_document_rechunk(
    doc_id: int,
    body: RechunkRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """선택한 규정집 원문으로 해당 doc_id에 대해 계층 청킹 + embedding_ko + search_tsv 재색인."""
    raw_text = (body.raw_text or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="raw_text is required and must be non-empty")
    doc = get_rag_document_detail(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="rag document not found")
    # 문서 메타(버전·시행일)를 청크에 반영 — 문서에 값이 있으면 청크 INSERT 시 함께 저장
    version = str(doc["version"]) if doc.get("version") is not None else None
    effective_from = doc.get("effective_from")
    effective_to = doc.get("effective_to")
    result = run_chunking_pipeline(
        db,
        doc_id,
        raw_text,
        version=version,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@app.get("/api/v1/agents")
def get_agents(db: Session = Depends(get_db)) -> dict[str, Any]:
    items = list_agents(db)
    return {"items": items, "total": len(items)}


@app.get("/api/v1/agents/{agent_id}")
def get_agent(agent_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = get_agent_detail(db, agent_id)
    if not item:
        raise HTTPException(status_code=404, detail="agent not found")
    return item


async def _run_analysis_task(
    *,
    run_id: str,
    case_id: str,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None,
    resume_value: dict[str, Any] | None = None,
    previous_result: dict[str, Any] | None = None,
    enable_hitl: bool = True,
) -> None:
    last_payload: dict[str, Any] | None = None
    voucher_key = case_id.replace("POC-", "")
    is_resume = resume_value is not None
    logger.info(
        "[analysis] _run_analysis_task run_id=%s case_id=%s resume=%s (resume_value keys=%s)",
        run_id,
        case_id,
        is_resume,
        list(resume_value.keys()) if isinstance(resume_value, dict) else None,
    )
    # 스트림이 "started"만 찍히고 한동안 비어 보이는 문제를 줄이기 위해,
    # 백엔드 태스크 시작 즉시 최소 1개의 AGENT_EVENT를 publish 한다.
    try:
        await runtime.publish(
            run_id,
            "AGENT_EVENT",
            {
                "event_type": "NODE_START",
                "node": "bootstrap",
                "phase": "system",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": "분석을 시작합니다." if not is_resume else "HITL 응답을 반영해 분석을 이어갑니다.",
                "metadata": {"resume": bool(is_resume)},
            },
        )
    except Exception:
        # 스트림 초기 이벤트 실패는 분석 자체를 막지 않는다.
        pass
    if is_resume:
        rv_keys = list(resume_value.keys())[:10] if isinstance(resume_value, dict) else []
        rv_approved = resume_value.get("approved") if isinstance(resume_value, dict) else None
        rv_comment = str(resume_value.get("comment") or "") if isinstance(resume_value, dict) else ""
        rv_comment_len = len(rv_comment)
        rv_comment_preview = (rv_comment[:80] + "…") if len(rv_comment) > 80 else rv_comment or "(없음)"
        logger.info(
            "[RESUME_TRACE] _run_analysis_task run_id=%s case_id=%s → 에이전트에 resume_value 전달 (keys=%s approved=%s comment_len=%s comment_preview=%s), checkpoint 재개 시도 예정",
            run_id, case_id, rv_keys, rv_approved, rv_comment_len, rv_comment_preview,
        )
    else:
        logger.info(
            "[RESUME_TRACE] _run_analysis_task run_id=%s case_id=%s → resume_value 없음, 스크리닝부터 전체 재실행",
            run_id, case_id,
        )
    try:
        _first_ev = True
        async for ev_type, ev_payload in run_agent_analysis(
            case_id,
            body_evidence=body_evidence,
            intended_risk_type=intended_risk_type,
            run_id=run_id,
            resume_value=resume_value,
            previous_result=previous_result,
            enable_hitl=enable_hitl,
        ):
            data = ev_payload if isinstance(ev_payload, dict) else {"value": ev_payload}
            if _first_ev:
                logger.info("[RESUME_TRACE] _run_analysis_task run_id=%s 첫 스트림 이벤트: ev_type=%s", run_id, ev_type)
                _first_ev = False
            if ev_type in ("completed", "failed"):
                logger.info(
                    "[RESUME_TRACE] _run_analysis_task run_id=%s 터미널 이벤트: ev_type=%s status=%s",
                    run_id, ev_type, (data.get("status") or data.get("result", {}).get("status") if isinstance(data.get("result"), dict) else None),
                )
            await runtime.publish(run_id, ev_type, data)

            # 분석 실행 중 스크리닝 결과가 나오면 agent_case에 반영 (한 번만)
            if ev_type == "AGENT_EVENT" and data.get("event_type") == "SCREENING_RESULT":
                meta = data.get("metadata") or {}
                ct = meta.get("case_type")
                sev = meta.get("severity")
                sc = meta.get("score")
                if ct and sev is not None and sc is not None:
                    try:
                        from db.session import SessionLocal
                        with SessionLocal() as persist_db:
                            upsert_agent_case_from_screening_result(
                                persist_db,
                                voucher_key,
                                case_type=ct,
                                severity=str(sev),
                                score=float(sc) / 100.0,
                                reason_text=str(data.get("observation") or data.get("message") or ""),
                            )
                    except Exception:
                        pass

            try:
                from db.session import SessionLocal
                with SessionLocal() as persist_db:
                    stage = data.get("phase") or data.get("node") or ev_type.lower()
                    stored_event_type = ev_type
                    if ev_type == "AGENT_EVENT" and data.get("event_type") == "HITL_REQUESTED":
                        stored_event_type = "HITL_REQUESTED"
                    elif ev_type == "completed":
                        stored_event_type = "HITL_REQUIRED" if data.get("status") == "HITL_REQUIRED" else "RUN_COMPLETED"
                    elif ev_type == "failed":
                        stored_event_type = "RUN_FAILED"
                    metadata = {
                        "stored_event_type": stored_event_type,
                        "payload": data,
                    }
                    if ev_type in {"completed", "failed"}:
                        metadata["result"] = data
                    log_run_event(
                        persist_db,
                        run_id=run_id,
                        case_id=case_id,
                        voucher_key=voucher_key,
                    stage=str(stage),
                    event_type=stored_event_type,
                    metadata=metadata,
                )
            except Exception:
                pass
            if ev_type == "completed":
                logger.info("[analysis] task ev_type=completed run_id=%s status=%s", run_id, data.get("status"))
                if data.get("status") == "HITL_REQUIRED":
                    logger.info("[analysis] HITL_REQUIRED run_id=%s — stream will pause, waiting for review-submit", run_id)
                    # HITL_REQUESTED 이벤트로 이미 전체 payload(reasons, review_questions 등)가 설정된 경우 유지.
                    # interrupt() value는 축약/직렬화된 형태일 수 있어 덮어쓰지 않음.
                    full_hitl = runtime.get_hitl_request(run_id)
                    if full_hitl:
                        data = {**data, "hitl_request": full_hitl}
                    elif not full_hitl:
                        runtime.set_hitl_request(run_id, data.get("hitl_request") or data)
                last_payload = {
                    "run_id": run_id,
                    "case_id": case_id,
                    "event_type": ev_type,
                    "result": data,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            elif str(ev_type).upper() == "HITL_REQUIRED":
                # 일부 실행 경로는 terminal event로 'completed' 대신 'HITL_REQUIRED'를 방출함.
                # 이 경우에도 result/status를 저장해야 UI/목록(case_status) 집계가 일관되게 동작한다.
                hitl_req = runtime.get_hitl_request(run_id) or data.get("hitl_request") or data
                if hitl_req:
                    try:
                        runtime.set_hitl_request(run_id, hitl_req)
                    except Exception:
                        pass
                src = (hitl_req or {}).get("source_summary") or {}
                last_payload = {
                    "run_id": run_id,
                    "case_id": case_id,
                    "event_type": "completed",
                    "result": {
                        "status": "HITL_REQUIRED",
                        "severity": src.get("severity"),
                        "score": src.get("score"),
                        "case_type": src.get("case_type"),
                        "hitl_request": hitl_req,
                    },
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            elif ev_type == "failed":
                last_payload = {
                    "run_id": run_id,
                    "case_id": case_id,
                    "event_type": ev_type,
                    "result": data,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
        # 에이전트가 종료 이벤트를 방출하지 않고 종료되는 경우, 스트림이 영원히 대기 상태로 남는다.
        # 이런 비정상 종료를 감지해 실패로 마감한다.
        if last_payload is None:
            fail_payload = {"error": "agent stream ended without terminal event", "stage": "runner"}
            try:
                await runtime.publish(run_id, "failed", fail_payload)
            except Exception:
                pass
            last_payload = {
                "run_id": run_id,
                "case_id": case_id,
                "event_type": "failed",
                "result": fail_payload,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
    except Exception as e:
        fail_payload = {"error": str(e), "stage": "runner"}
        await runtime.publish(run_id, "failed", fail_payload)
        try:
            from db.session import SessionLocal
            with SessionLocal() as persist_db:
                log_run_event(
                    persist_db,
                    run_id=run_id,
                    case_id=case_id,
                    voucher_key=voucher_key,
                    stage="runner",
                    event_type="RUN_FAILED",
                    metadata={"stored_event_type": "RUN_FAILED", "payload": fail_payload, "result": fail_payload},
                )
        except Exception:
            pass
        last_payload = {
            "run_id": run_id,
            "case_id": case_id,
            "event_type": "failed",
            "result": fail_payload,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        if last_payload is not None:
            runtime.set_result(run_id, last_payload)
            try:
                from db.session import SessionLocal
                with SessionLocal() as persist_db:
                    persist_analysis_result(persist_db, run_id=run_id, result_payload=last_payload)
                    run_status = (last_payload.get("result") or {}).get("status")
                    update_agent_case_status_from_run(persist_db, voucher_key, run_status)
            except Exception as e:
                logger.warning("persist_analysis_result failed run_id=%s case_id=%s error=%s", run_id, case_id, e)

            try:
                if last_payload.get("result", {}).get("status") != "HITL_REQUIRED":
                    from db.session import SessionLocal
                    with SessionLocal() as persist_db:
                        events = runtime.get_timeline(run_id)
                        result = runtime.get_result(run_id)
                        lineage = runtime.get_lineage(run_id)
                        hitl_req = runtime.get_hitl_request(run_id)
                        hitl_res = runtime.get_hitl_response(run_id)
                        diag = get_run_diagnostics(
                            result=result or {},
                            timeline=events or [],
                            lineage=lineage,
                            hitl_request=hitl_req,
                            hitl_response=hitl_res,
                        )
                        log_run_event(
                            persist_db,
                            run_id=run_id,
                            case_id=case_id,
                            voucher_key=voucher_key,
                            stage="diagnostics",
                            event_type="RUN_DIAGNOSTICS_SNAPSHOT",
                            metadata={"stored_event_type": "RUN_DIAGNOSTICS_SNAPSHOT", "diagnostics": diag},
                        )
            except Exception as e:
                logger.warning("diagnostics snapshot persist failed run_id=%s case_id=%s error=%s", run_id, case_id, e)
        # 분석이 끝나면 스트림에 "done"을 보내 클라이언트가 대기에서 빠지도록 항상 close 호출.
        # close()는 큐에 "done"만 넣고 큐/run 컨텍스트는 유지하므로, HITL/증빙 재개 시 같은 run_id로 이어서 사용 가능.
        if last_payload is not None:
            final_status = (last_payload.get("result") or {}).get("status")
            logger.info("[analysis] task done run_id=%s final_status=%s — closing stream (done)", run_id, final_status)
            await runtime.close(run_id)


@app.post("/api/v1/cases/{voucher_key}/screen")
def screen_voucher(voucher_key: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """
    Phase 0 — Screening.
    Classifies a raw voucher into a case type using deterministic signal analysis.
    Creates or updates an AgentCase row with the result.
    Must be called before analysis-runs for proper case type assignment.
    """
    try:
        result = run_case_screening(db, voucher_key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return result


@app.post("/api/v1/cases/{voucher_key}/analysis-runs", response_model=AnalysisStartResponse)
async def start_analysis(
    voucher_key: str,
    body: AnalysisStartRequest | None = Body(None),
    db: Session = Depends(get_db),
) -> AnalysisStartResponse:
    try:
        payload = build_analysis_payload(db, voucher_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    run_id = str(uuid.uuid4())
    case_id = payload["case_id"]
    enable_hitl = body.enable_hitl if body is not None else True
    logger.info("[analysis] start_analysis voucher_key=%s run_id=%s case_id=%s enable_hitl=%s", voucher_key, run_id, case_id, enable_hitl)
    runtime.create_run(case_id=case_id, run_id=run_id, mode="primary")
    try:
        create_analysis_run_row(
            db,
            run_id=run_id,
            case_id_int=payload["case_id_int"],
        )
    except Exception:
        pass
    try:
        log_run_event(
            db,
            run_id=run_id,
            case_id=case_id,
            voucher_key=voucher_key,
            stage="start",
            event_type="RUN_CREATED",
            metadata={
                "stored_event_type": "RUN_CREATED",
                "lineage": runtime.get_lineage(run_id),
                "body_evidence": payload["body_evidence"],
            },
        )
    except Exception:
        pass
    asyncio.create_task(
        _run_analysis_task(
            run_id=run_id,
            case_id=case_id,
            body_evidence=payload["body_evidence"],
            intended_risk_type=payload.get("intended_risk_type"),
            enable_hitl=enable_hitl,
        )
    )
    return AnalysisStartResponse(
        accepted=True,
        run_id=run_id,
        case_id=case_id,
        stream_path=f"/api/v1/analysis-runs/{run_id}/stream",
    )


@app.post("/api/v1/analysis-runs/{run_id}/hitl", response_model=HitlSubmitResponse)
async def submit_hitl(run_id: str, request: HitlSubmitRequest, db: Session = Depends(get_db)) -> HitlSubmitResponse:
    # 재시작 후에도 HITL 응답을 받을 수 있도록, runtime 메모리와 DB(aux_state)를 모두 확인한다.
    lineage = runtime.get_lineage(run_id)
    aux = get_run_aux_state(db, run_id=run_id)
    if not lineage and not aux.get("lineage"):
        raise HTTPException(status_code=404, detail="source run not found")

    lineage = lineage or aux.get("lineage") or {}
    _ensure_runtime_resume_context(run_id, lineage)

    hitl_request = runtime.get_hitl_request(run_id) or aux.get("hitl_request")
    if not hitl_request:
        raise HTTPException(status_code=400, detail="no pending HITL request for this run")
    # runtime 메모리에 없던 경우 aux에서 복원
    if not runtime.get_hitl_request(run_id):
        runtime.set_hitl_request(run_id, hitl_request)

    case_id = lineage["case_id"]
    voucher_key = case_id.replace("POC-", "")
    try:
        payload = build_analysis_payload(db, voucher_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    hitl_payload = request.model_dump()
    runtime.set_hitl_response(run_id, hitl_payload)
    try:
        log_run_event(
            db,
            run_id=run_id,
            case_id=case_id,
            voucher_key=voucher_key,
            stage="hitl",
            event_type="HITL_RESPONSE",
            metadata={"stored_event_type": "HITL_RESPONSE", "hitl_response": hitl_payload},
        )
    except Exception:
        pass

    # 정식 HITL: 같은 run_id(thread_id)로 재개. 새 run 생성 없음.
    body_evidence = dict(payload["body_evidence"] or {})
    body_evidence["hitlRequest"] = hitl_request
    asyncio.create_task(
        _run_analysis_task(
            run_id=run_id,
            case_id=case_id,
            body_evidence=body_evidence,
            intended_risk_type=payload.get("intended_risk_type"),
            resume_value=hitl_payload,
        )
    )

    return HitlSubmitResponse(
        accepted=True,
        source_run_id=run_id,
        resumed_run_id=run_id,
        stream_path=f"/api/v1/analysis-runs/{run_id}/stream",
    )


@app.post("/api/v1/analysis-runs/{run_id}/hitl-draft")
async def save_hitl_draft(run_id: str, request: HitlDraftRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    lineage = runtime.get_lineage(run_id)
    aux = get_run_aux_state(db, run_id=run_id)
    if not lineage and not aux.get("lineage"):
        raise HTTPException(status_code=404, detail="source run not found")

    lineage = lineage or aux.get("lineage") or {}
    case_id = lineage.get("case_id")
    voucher_key = case_id.replace("POC-", "") if case_id else None
    hitl_draft = request.model_dump()
    runtime.set_hitl_draft(run_id, hitl_draft)
    try:
        log_run_event(
            db,
            run_id=run_id,
            case_id=case_id or "-",
            voucher_key=voucher_key,
            stage="hitl",
            event_type="HITL_DRAFT",
            metadata={"stored_event_type": "HITL_DRAFT", "hitl_draft": hitl_draft},
        )
    except Exception:
        pass
    return {"accepted": True, "run_id": run_id, "hitl_draft": hitl_draft}


@app.post("/api/v1/analysis-runs/{run_id}/evidence-upload")
async def evidence_upload(
    run_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Phase 1–2: REVIEW_REQUIRED인 run에 대해 증빙 파일 업로드 → 저장·추출·전표 비교 후
    evidence_document_result를 agent_activity_log에 기록. 이후 evidence-resume으로 완료 처리.
    """
    lineage = runtime.get_lineage(run_id)
    aux = get_run_aux_state(db, run_id=run_id)
    if not lineage and not aux.get("lineage"):
        raise HTTPException(status_code=404, detail="source run not found")
    lineage = lineage or aux.get("lineage") or {}
    case_id = lineage.get("case_id")
    voucher_key = (case_id or "").replace("POC-", "")
    if not voucher_key:
        raise HTTPException(status_code=400, detail="voucher_key not found for run")
    current_status = get_agent_case_status(db, voucher_key)
    if current_status not in ("REVIEW_REQUIRED", "EVIDENCE_REJECTED"):
        raise HTTPException(
            status_code=400,
            detail=f"evidence upload only when case status is REVIEW_REQUIRED or EVIDENCE_REJECTED (current: {current_status})",
        )
    try:
        payload = build_analysis_payload(db, voucher_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    body_evidence = payload.get("body_evidence") or {}
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")
    extracted, sha256_hex, _ = extract_from_bytes(content, run_id, file.filename or "upload")
    comparison = compare_evidence_to_voucher(extracted, body_evidence)
    evidence_document_result = {
        "passed": comparison.passed,
        "confidence": comparison.confidence,
        "reasons": comparison.reasons,
        "extracted_fields": comparison.extracted_fields,
        "comparison_detail": comparison.comparison_detail,
        "mismatches": comparison.mismatches,
        "file_sha256": sha256_hex,
        "filename": file.filename,
    }
    try:
        log_run_event(
            db,
            run_id=run_id,
            case_id=case_id,
            voucher_key=voucher_key,
            stage="evidence",
            event_type="EVIDENCE_UPLOADED",
            metadata={
                "stored_event_type": "EVIDENCE_UPLOADED",
                "evidence_document_result": evidence_document_result,
            },
        )
    except Exception:
        pass
    return {
        "accepted": True,
        "run_id": run_id,
        "evidence_document_result": evidence_document_result,
    }


@app.post("/api/v1/analysis-runs/{run_id}/evidence-resume")
async def evidence_resume(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """
    Phase 3: 업로드·비교된 evidence_document_result를 기준으로 최종 결과 확정.
    - 증빙 불일치: 기존 분석에 따른 추가 분석 없이 EVIDENCE_REJECTED로 종료.
    - 증빙 일치: 기존 에이전트 분석 결과(score_breakdown, tool_results, policy_refs 등)를 유지한 채
      status=COMPLETED_AFTER_EVIDENCE, evidenceDocumentResult만 반영해 이어서 확정.
    """
    lineage = runtime.get_lineage(run_id)
    aux = get_run_aux_state(db, run_id=run_id)
    if not lineage and not aux.get("lineage"):
        raise HTTPException(status_code=404, detail="source run not found")
    lineage = lineage or aux.get("lineage") or {}
    case_id = lineage.get("case_id")
    voucher_key = (case_id or "").replace("POC-", "")
    evidence_result = aux.get("evidence_document_result")
    if not evidence_result:
        raise HTTPException(status_code=400, detail="no evidence upload result for this run; upload evidence first")
    passed = evidence_result.get("passed") is True
    status = "COMPLETED_AFTER_EVIDENCE" if passed else "EVIDENCE_REJECTED"
    reasons = evidence_result.get("reasons") or []
    reason_text_evidence = "; ".join(reasons) if reasons else ("증빙 검증 통과" if passed else "증빙 불일치")

    # 기존 run 결과(에이전트 분석)가 있으면 유지.
    # 증빙 불일치: 추가 분석 없이 status만 EVIDENCE_REJECTED로 확정(기존 분석 내용은 유지).
    # 증빙 일치: 기존 분석에 이어서 status=COMPLETED_AFTER_EVIDENCE, evidenceDocumentResult 반영.
    existing = runtime.get_result(run_id) or aux.get("result_payload")
    base_result = (existing or {}).get("result") if isinstance(existing, dict) else {}
    if base_result and (base_result.get("tool_results") or base_result.get("score_breakdown")):
        new_result = dict(base_result)
        new_result["status"] = status
        new_result["evidenceDocumentResult"] = evidence_result
        new_result["reasonText"] = (str(new_result.get("reasonText") or "").strip() + " " + reason_text_evidence).strip()
        new_result.setdefault("score_breakdown", new_result.get("score_breakdown") or {})
        new_result.setdefault("tool_results", new_result.get("tool_results") or [])
        result_payload = {"result": new_result}
    else:
        result_payload = {
            "result": {
                "status": status,
                "reasonText": reason_text_evidence,
                "severity": "LOW" if passed else "MEDIUM",
                "score": 100 if passed else 50,
                "score_breakdown": {},
                "tool_results": [],
                "quality_gate_codes": [],
                "evidenceDocumentResult": evidence_result,
            },
        }
    try:
        log_run_event(
            db,
            run_id=run_id,
            case_id=case_id,
            voucher_key=voucher_key,
            stage="evidence",
            event_type="RUN_COMPLETED",
            metadata={"stored_event_type": "RUN_COMPLETED", "result": result_payload},
        )
        persist_analysis_result(db, run_id=run_id, result_payload=result_payload)
        update_agent_case_status_from_run(db, voucher_key, status)
        runtime.set_result(run_id, result_payload)
    except Exception as e:
        logger.exception("evidence_resume persist failed run_id=%s", run_id)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {
        "accepted": True,
        "run_id": run_id,
        "status": status,
        "result": result_payload.get("result"),
    }


@app.post("/api/v1/analysis-runs/{run_id}/review-submit")
async def review_submit(
    run_id: str,
    request: ReviewSubmitRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    HITL 팝업 통합 제출. 팝업의 모든 내용(HITL 응답 + 증빙 업로드 여부)을 받아
    에이전트가 필수 항목·조건을 판단한 뒤 분석을 이어가거나 증빙만 확정한다.
    """
    logger.info(
        "[RESUME_TRACE] review_submit 진입: run_id=%s hitl_response=%s evidence_uploaded=%s",
        run_id, request.hitl_response is not None, getattr(request, "evidence_uploaded", None),
    )
    lineage = runtime.get_lineage(run_id)
    aux = get_run_aux_state(db, run_id=run_id)
    if not lineage and not aux.get("lineage"):
        raise HTTPException(status_code=404, detail="source run not found")
    lineage = lineage or aux.get("lineage") or {}
    _ensure_runtime_resume_context(run_id, lineage)
    case_id = lineage["case_id"]
    voucher_key = case_id.replace("POC-", "")
    logger.info("[RESUME_TRACE] review_submit lineage 확보: run_id=%s case_id=%s voucher_key=%s", run_id, case_id, voucher_key)
    # runtime/aux에 HITL_REQUESTED 이벤트가 없어도, 완료 결과(result_payload)에 hitl_request가 있으면 사용(팝업 제출 400 방지)
    base_result_payload = runtime.get_result(run_id) or aux.get("result_payload") or {}
    base_result = (
        base_result_payload.get("result")
        if isinstance(base_result_payload, dict) and isinstance(base_result_payload.get("result"), dict)
        else (base_result_payload if isinstance(base_result_payload, dict) else {})
    )
    hitl_request = (
        runtime.get_hitl_request(run_id)
        or aux.get("hitl_request")
        or (base_result.get("hitl_request") if isinstance(base_result.get("hitl_request"), dict) else None)
    )
    evidence_result = aux.get("evidence_document_result") if request.evidence_uploaded else None

    # hitl_request가 있는데 body에 hitl_response가 없으면 최소 payload로 진행(400 방지). 판단은 이후 LLM이 수행.
    if hitl_request and request.hitl_response is None:
        from services.schemas import HitlSubmitRequest
        request = request.model_copy(update={"hitl_response": HitlSubmitRequest(approved=True, comment="")})

    if hitl_request and request.hitl_response is not None:
        hitl_payload = request.hitl_response.model_dump()
        runtime.set_hitl_response(run_id, hitl_payload)
        try:
            log_run_event(
                db,
                run_id=run_id,
                case_id=case_id,
                voucher_key=voucher_key,
                stage="hitl",
                event_type="HITL_RESPONSE",
                metadata={"stored_event_type": "HITL_RESPONSE", "hitl_response": hitl_payload},
            )
        except Exception:
            pass
        if not runtime.get_hitl_request(run_id):
            runtime.set_hitl_request(run_id, hitl_request)
        try:
            payload = build_analysis_payload(db, voucher_key)
        except Exception as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        body_evidence = dict(payload.get("body_evidence") or {})
        # REVIEW_REQUIRED 경로는 interrupt(resume) 대상 run이 아닐 수 있으므로,
        # body_evidence에 HITL 응답을 명시적으로 주입해 재분석 입력으로 항상 전달한다.
        body_evidence["hitlResponse"] = hitl_payload
        body_evidence["hitlRequest"] = hitl_request
        if evidence_result:
            body_evidence["evidenceDocumentResult"] = evidence_result
        # HITL_REQUIRED(실제 interrupt 대기)에서만 Command(resume=...)로 재개.
        # base_result.status가 HITL_REQUIRED가 아니어도 runtime에 hitl_request가 있으면 재개 시도(체크포인트 없으면 2차 경로로 fallback).
        base_status = str((base_result or {}).get("status") or "").upper()
        has_runtime_hitl = bool(runtime.get_hitl_request(run_id))
        use_resume_value = hitl_payload if (base_status == "HITL_REQUIRED" or has_runtime_hitl) else None
        path_kind = "1차(checkpoint 재개)" if use_resume_value is not None else "2차(처음부터 재실행)"
        logger.info(
            "[RESUME_TRACE] review_submit 경로 결정: run_id=%s base_status=%s has_runtime_hitl=%s → %s",
            run_id, base_status, has_runtime_hitl, path_kind,
        )
        logger.info(
            "[analysis] review_submit run_id=%s base_status=%s use_resume_value=%s (will resume from checkpoint=%s)",
            run_id,
            base_status,
            "yes" if use_resume_value else "no",
            use_resume_value is not None,
        )
        if use_resume_value is not None:
            logger.info(
                "[RESUME_TRACE] review_submit run_id=%s → 1차: Command(resume=...) 사용 (execute 재실행 없음 예상)",
                run_id,
            )
        else:
            logger.info(
                "[RESUME_TRACE] review_submit run_id=%s → 2차: body_evidence에 hitlResponse 주입 후 스크리닝부터 (execute 재실행 예상)",
                run_id,
            )
        runtime.drain_queue_before_resume(run_id)
        logger.info(
            "[RESUME_TRACE] review_submit _run_analysis_task 스케줄: run_id=%s case_id=%s path=%s body_evidence_keys=%s",
            run_id, case_id, path_kind, list(body_evidence.keys())[:15] if isinstance(body_evidence, dict) else [],
        )
        asyncio.create_task(
            _run_analysis_task(
                run_id=run_id,
                case_id=case_id,
                body_evidence=body_evidence,
                intended_risk_type=payload.get("intended_risk_type"),
                resume_value=use_resume_value,
                previous_result=base_result,
            )
        )
        await asyncio.sleep(0)
        return {
            "accepted": True,
            "source_run_id": run_id,
            "resumed_run_id": run_id,
            "stream_path": f"/api/v1/analysis-runs/{run_id}/stream",
        }

    if not hitl_request and evidence_result:
        passed = evidence_result.get("passed") is True
        status = "COMPLETED_AFTER_EVIDENCE" if passed else "EVIDENCE_REJECTED"
        reasons = evidence_result.get("reasons") or []
        reason_text_evidence = "; ".join(reasons) if reasons else ("증빙 검증 통과" if passed else "증빙 불일치")
        existing = runtime.get_result(run_id) or aux.get("result_payload")
        base_result = (existing or {}).get("result") if isinstance(existing, dict) else {}
        if base_result and (base_result.get("tool_results") or base_result.get("score_breakdown")):
            new_result = dict(base_result)
            new_result["status"] = status
            new_result["evidenceDocumentResult"] = evidence_result
            new_result["reasonText"] = (str(new_result.get("reasonText") or "").strip() + " " + reason_text_evidence).strip()
            new_result.setdefault("score_breakdown", new_result.get("score_breakdown") or {})
            new_result.setdefault("tool_results", new_result.get("tool_results") or [])
            result_payload = {"result": new_result}
        else:
            result_payload = {
                "result": {
                    "status": status,
                    "reasonText": reason_text_evidence,
                    "severity": "LOW" if passed else "MEDIUM",
                    "score": 100 if passed else 50,
                    "score_breakdown": {},
                    "tool_results": [],
                    "quality_gate_codes": [],
                    "evidenceDocumentResult": evidence_result,
                },
            }
        try:
            log_run_event(
                db,
                run_id=run_id,
                case_id=case_id,
                voucher_key=voucher_key,
                stage="evidence",
                event_type="RUN_COMPLETED",
                metadata={"stored_event_type": "RUN_COMPLETED", "result": result_payload},
            )
            persist_analysis_result(db, run_id=run_id, result_payload=result_payload)
            update_agent_case_status_from_run(db, voucher_key, status)
            runtime.set_result(run_id, result_payload)
        except Exception as e:
            logger.exception("review_submit evidence_resume persist failed run_id=%s", run_id)
            raise HTTPException(status_code=500, detail=str(e)) from e
        return {
            "accepted": True,
            "run_id": run_id,
            "status": status,
            "stream_path": f"/api/v1/analysis-runs/{run_id}/stream",
        }

    if hitl_request:
        raise HTTPException(status_code=400, detail="hitl_response required when HITL is pending")
    raise HTTPException(
        status_code=400,
        detail="no evidence upload for this run; upload evidence or submit HITL response",
    )


@app.get("/api/v1/cases/{voucher_key}/analysis/latest")
def get_latest_analysis(voucher_key: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        payload = build_analysis_payload(db, voucher_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    case_id = payload["case_id"]
    run_id = runtime.latest_run_of_case(case_id) or get_latest_run_id_by_case(db, case_id=case_id)
    if not run_id:
        return {"case_id": case_id, "run_id": None, "result": None}

    result = runtime.get_result(run_id)
    aux = get_run_aux_state(db, run_id=run_id)
    if result is None and aux.get("result_payload") is not None:
        result = {
            "run_id": run_id,
            "case_id": case_id,
            "event_type": "completed",
            "result": aux.get("result_payload"),
        }
    hitl_request = runtime.get_hitl_request(run_id) or aux.get("hitl_request")
    # 최신 run 상태를 AgentCase.status에 반영해 vouchers 목록/KPI가 즉시 일관되게 보이도록 함
    try:
        derived_status = None
        final_payload = {}
        if isinstance(result, dict) and isinstance(result.get("result"), dict):
            final_payload = result.get("result") or {}
        elif isinstance(aux.get("result_payload"), dict):
            final_payload = aux.get("result_payload") or {}
        derived_status = final_payload.get("status")
        if not derived_status and hitl_request:
            derived_status = "HITL_REQUIRED"
        update_agent_case_status_from_run(db, voucher_key, derived_status)
    except Exception:
        pass
    # UI에서 result.result.status가 None이면 오류 방지: 내부 result에 status가 없으면 derived_status 또는 IN_PROGRESS 보정
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        inner = result.get("result") or {}
        if inner.get("status") is None:
            result = dict(result)
            result["result"] = dict(inner)
            result["result"]["status"] = derived_status or "IN_PROGRESS"
    return {
        "case_id": case_id,
        "run_id": run_id,
        "result": result,
        "timeline_count": len(runtime.get_timeline(run_id)) or len(get_persisted_timeline(db, run_id=run_id)),
        "hitl_request": hitl_request,
        "hitl_draft": runtime.get_hitl_draft(run_id) or aux.get("hitl_draft"),
        "hitl_response": runtime.get_hitl_response(run_id) or aux.get("hitl_response"),
        "lineage": runtime.get_lineage(run_id) or aux.get("lineage"),
        "evidence_document_result": aux.get("evidence_document_result"),
    }


@app.get("/api/v1/cases/{voucher_key}/analysis/history")
def get_analysis_history(voucher_key: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        payload = build_analysis_payload(db, voucher_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    case_id = payload["case_id"]
    run_ids = runtime.list_runs_of_case(case_id) or list_run_ids_by_case(db, case_id=case_id)
    items = []
    for run_id in run_ids:
        result = runtime.get_result(run_id) or {}
        aux = get_run_aux_state(db, run_id=run_id)
        final = result.get("result") or aux.get("result_payload") or {}
        items.append({
            "run_id": run_id,
            "status": final.get("status"),
            "severity": final.get("severity"),
            "score": final.get("score"),
            "reasonText": final.get("reasonText"),
            "lineage": runtime.get_lineage(run_id) or aux.get("lineage"),
            "hitl_request": runtime.get_hitl_request(run_id) or aux.get("hitl_request"),
            "hitl_draft": runtime.get_hitl_draft(run_id) or aux.get("hitl_draft"),
            "hitl_response": runtime.get_hitl_response(run_id) or aux.get("hitl_response"),
        })
    return {"case_id": case_id, "items": items}


@app.get("/api/v1/analysis-runs/{run_id}/events")
def get_run_events(run_id: str) -> dict[str, Any]:
    from db.session import SessionLocal
    events = runtime.get_timeline(run_id)
    result = runtime.get_result(run_id)
    hitl_request = runtime.get_hitl_request(run_id)
    hitl_draft = runtime.get_hitl_draft(run_id)
    hitl_response = runtime.get_hitl_response(run_id)
    lineage = runtime.get_lineage(run_id)
    evidence_document_result = None
    if not events or result is None or lineage is None:
        with SessionLocal() as db:
            if not events:
                events = get_persisted_timeline(db, run_id=run_id)
            aux = get_run_aux_state(db, run_id=run_id)
            if result is None and aux.get("result_payload") is not None:
                result = {"run_id": run_id, "event_type": "completed", "result": aux.get("result_payload")}
            hitl_request = hitl_request or aux.get("hitl_request")
            hitl_draft = hitl_draft or aux.get("hitl_draft")
            hitl_response = hitl_response or aux.get("hitl_response")
            lineage = lineage or aux.get("lineage")
            evidence_document_result = aux.get("evidence_document_result")
    else:
        with SessionLocal() as db:
            aux = get_run_aux_state(db, run_id=run_id)
            evidence_document_result = aux.get("evidence_document_result")
    return {
        "run_id": run_id,
        "events": events,
        "event_count": len(events),
        "result": result,
        "hitl_request": hitl_request,
        "hitl_draft": hitl_draft,
        "hitl_response": hitl_response,
        "lineage": lineage,
        "evidence_document_result": evidence_document_result,
    }


@app.get("/api/v1/cases/{voucher_key}/runs/compare")
def compare_case_runs(
    voucher_key: str,
    run_ids: str = Query(..., description="comma-separated run_ids"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Phase H: 케이스 내 여러 run의 진단 지표 비교."""
    try:
        payload = build_analysis_payload(db, voucher_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    case_id = payload["case_id"]
    allowed = set(runtime.list_runs_of_case(case_id) or list_run_ids_by_case(db, case_id=case_id))
    ids = [r.strip() for r in run_ids.split(",") if r.strip()][:10]
    ids = [r for r in ids if r in allowed]
    diagnostics_list = []
    for run_id in ids:
        events = runtime.get_timeline(run_id)
        result = runtime.get_result(run_id)
        lineage = runtime.get_lineage(run_id)
        hitl_request = runtime.get_hitl_request(run_id)
        hitl_response = runtime.get_hitl_response(run_id)
        if not events:
            events = get_persisted_timeline(db, run_id=run_id)
        aux = get_run_aux_state(db, run_id=run_id)
        if result is None and aux.get("result_payload"):
            result = {"run_id": run_id, "event_type": "completed", "result": aux.get("result_payload")}
        lineage = lineage or aux.get("lineage")
        hitl_request = hitl_request or aux.get("hitl_request")
        hitl_response = hitl_response or aux.get("hitl_response")
        diagnostics_list.append(
            get_run_diagnostics(
                result=result or {"run_id": run_id},
                timeline=events,
                lineage=lineage,
                hitl_request=hitl_request,
                hitl_response=hitl_response,
            )
        )
    return compare_runs_diagnostics(diagnostics_list)


@app.get("/api/v1/analysis-runs/{run_id}/diagnostics")
def get_run_diagnostics_endpoint(run_id: str) -> dict[str, Any]:
    """Phase H: run 단위 관찰 지표 (tool success, HITL, citation coverage, fallback rate)."""
    from db.session import SessionLocal

    events = runtime.get_timeline(run_id)
    result = runtime.get_result(run_id)
    lineage = runtime.get_lineage(run_id)
    hitl_request = runtime.get_hitl_request(run_id)
    hitl_response = runtime.get_hitl_response(run_id)
    if not events:
        with SessionLocal() as db:
            events = get_persisted_timeline(db, run_id=run_id)
    with SessionLocal() as db:
        aux = get_run_aux_state(db, run_id=run_id)
        if result is None and aux.get("result_payload") is not None:
            result = {"run_id": run_id, "event_type": "completed", "result": aux.get("result_payload")}
        lineage = lineage or aux.get("lineage")
        hitl_request = hitl_request or aux.get("hitl_request")
        hitl_response = hitl_response or aux.get("hitl_response")
    if result is None and not lineage and not events:
        raise HTTPException(status_code=404, detail="run not found")
    return get_run_diagnostics(
        result=result or {"run_id": run_id},
        timeline=events,
        lineage=lineage,
        hitl_request=hitl_request,
        hitl_response=hitl_response,
    )


@app.get("/api/v1/analysis-runs/{run_id}/stream")
async def stream_analysis(run_id: str):
    q = runtime.get_queue(run_id)
    if q is None:
        raise HTTPException(status_code=404, detail="run_id not found")

    async def event_gen():
        yield "event: started\ndata: {}\n\n"
        while True:
            ev_type, payload = await q.get()
            if ev_type == "done":
                yield "event: completed\ndata: [DONE]\n\n"
                break
            data = json.dumps(payload, ensure_ascii=False, default=str)
            yield f"event: {ev_type}\ndata: {data}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/demo/seed")
def demo_seed(
    scenario: str = Query("HOLIDAY_USAGE"),
    count: int = Query(5, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return seed_demo_scenarios(db, scenario=scenario, count=count)


@app.get("/api/v1/demo/scenarios")
def demo_scenarios() -> dict[str, Any]:
    return {"items": list_demo_scenarios()}


@app.get("/api/v1/demo/seeded")
def demo_seeded(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": list_seeded_demo_cases(db)}


@app.delete("/api/v1/demo/seed")
def demo_seed_clear(db: Session = Depends(get_db)) -> dict[str, Any]:
    return clear_demo_data(db)
