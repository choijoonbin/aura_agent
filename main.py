from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from agent.aura_bridge import run_agent_analysis
from db.session import get_db
from services.agent_studio_service import get_agent_detail, list_agents
from services.case_service import (
    build_analysis_payload,
    list_vouchers,
    run_case_screening,
    upsert_agent_case_from_screening_result,
)
from services.demo_data_service import clear_demo_data, list_demo_scenarios, list_seeded_demo_cases, seed_demo_scenarios
from services.persistence_service import persist_analysis_result
from services.chunking_pipeline import run_chunking_pipeline
from services.rag_library_service import get_rag_document_detail, list_rag_documents
from services.run_diagnostics import compare_runs_diagnostics, get_run_diagnostics
from services.runtime_persistence_service import (
    get_latest_run_id_by_case,
    get_persisted_timeline,
    get_run_aux_state,
    list_run_ids_by_case,
    log_run_event,
)
from services.schemas import AnalysisStartResponse, HitlDraftRequest, HitlSubmitRequest, HitlSubmitResponse
from services.stream_runtime import runtime
from utils.config import ensure_source_paths, settings


logger = logging.getLogger(__name__)

app = FastAPI(title="AuraAgent PoC API", version="0.3.0")


@app.on_event("startup")
async def startup() -> None:
    ensure_source_paths()


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
    rows = list_vouchers(db, queue=queue, limit=limit)
    return {"items": [r.model_dump() for r in rows], "total": len(rows)}


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
) -> None:
    last_payload: dict[str, Any] | None = None
    voucher_key = case_id.replace("POC-", "")
    try:
        async for ev_type, ev_payload in run_agent_analysis(
            case_id,
            body_evidence=body_evidence,
            intended_risk_type=intended_risk_type,
            run_id=run_id,
            resume_value=resume_value,
        ):
            data = ev_payload if isinstance(ev_payload, dict) else {"value": ev_payload}
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
                last_payload = {
                    "run_id": run_id,
                    "case_id": case_id,
                    "event_type": ev_type,
                    "result": data,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                if data.get("status") == "HITL_REQUIRED":
                    runtime.set_hitl_request(run_id, data.get("hitl_request") or data)
            elif ev_type == "failed":
                last_payload = {
                    "run_id": run_id,
                    "case_id": case_id,
                    "event_type": ev_type,
                    "result": data,
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
        # HITL_REQUIRED면 같은 run으로 재개 가능하므로 close 하지 않음
        if last_payload is None or last_payload.get("result", {}).get("status") != "HITL_REQUIRED":
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
async def start_analysis(voucher_key: str, db: Session = Depends(get_db)) -> AnalysisStartResponse:
    try:
        payload = build_analysis_payload(db, voucher_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    run_id = str(uuid.uuid4())
    case_id = payload["case_id"]
    runtime.create_run(case_id=case_id, run_id=run_id, mode="primary")
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
    lineage = runtime.get_lineage(run_id)
    if not lineage:
        raise HTTPException(status_code=404, detail="source run not found")

    hitl_request = runtime.get_hitl_request(run_id)
    if not hitl_request:
        raise HTTPException(status_code=400, detail="no pending HITL request for this run")

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
    asyncio.create_task(
        _run_analysis_task(
            run_id=run_id,
            case_id=case_id,
            body_evidence=payload["body_evidence"],
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
    return {
        "case_id": case_id,
        "run_id": run_id,
        "result": result,
        "timeline_count": len(runtime.get_timeline(run_id)) or len(get_persisted_timeline(db, run_id=run_id)),
        "hitl_request": runtime.get_hitl_request(run_id) or aux.get("hitl_request"),
        "hitl_draft": runtime.get_hitl_draft(run_id) or aux.get("hitl_draft"),
        "hitl_response": runtime.get_hitl_response(run_id) or aux.get("hitl_response"),
        "lineage": runtime.get_lineage(run_id) or aux.get("lineage"),
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
    return {
        "run_id": run_id,
        "events": events,
        "event_count": len(events),
        "result": result,
        "hitl_request": hitl_request,
        "hitl_draft": hitl_draft,
        "hitl_response": hitl_response,
        "lineage": lineage,
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
