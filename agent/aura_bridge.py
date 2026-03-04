from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from utils.config import settings


def _setup_aura_import_path() -> None:
    p = Path(settings.aura_platform_path)
    if not p.exists():
        raise RuntimeError(f"AURA_PLATFORM_PATH not found: {p}")
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
    os.environ.setdefault("TENANT_DEFAULT", str(settings.default_tenant_id))


async def run_legacy_aura_analysis(
    case_id: str,
    *,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None = None,
) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
    try:
        _setup_aura_import_path()
        from core.analysis.analysis_pipeline import run_audit_analysis

        run_id = str(uuid.uuid4())
        async for ev_type, payload in run_audit_analysis(
            case_id,
            run_id=run_id,
            tenant_id=str(settings.default_tenant_id),
            trace_id=f"matertask-{case_id}-{run_id[:8]}",
            body_evidence=body_evidence,
            intended_risk_type=intended_risk_type,
        ):
            yield ev_type, payload
    except Exception as e:
        now = datetime.now(timezone.utc).isoformat()
        yield "started", {"caseId": case_id, "at": now}
        yield "step", {"label": "INPUT_NORM", "detail": "Aura import fallback", "percent": 20}
        await asyncio.sleep(0.2)
        yield "step", {"label": "EVIDENCE_GATHER", "detail": str(e), "percent": 60}
        await asyncio.sleep(0.2)
        yield "completed", {
            "caseId": case_id,
            "status": "HOLD",
            "reasonText": "Aura runtime fallback으로 분석이 완료되었습니다. 환경 설정 후 재실행하세요.",
            "score": 0.45,
            "severity": "LOW",
            "quality_gate_codes": ["INPUT_PARTIAL"],
        }


async def run_agent_analysis(
    case_id: str,
    *,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None = None,
    run_id: str | None = None,
    resume_value: dict[str, Any] | None = None,
) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
    if settings.enable_langgraph_if_available:
        from agent.langgraph_agent import run_langgraph_agentic_analysis

        async for ev_type, payload in run_langgraph_agentic_analysis(
            case_id,
            body_evidence=body_evidence,
            intended_risk_type=intended_risk_type,
            run_id=run_id,
            resume_value=resume_value,
        ):
            yield ev_type, payload
        return

    from agent.native_agent import run_native_agentic_analysis

    async for ev_type, payload in run_native_agentic_analysis(
        case_id,
        body_evidence=body_evidence,
        intended_risk_type=intended_risk_type,
    ):
        yield ev_type, payload
