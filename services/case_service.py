from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from agent.screener import run_screening
from db.models import AgentCase, FiDocHeader, FiDocItem
from services.demo_data_service import SCENARIO_PROFILES
from services.schemas import VoucherRow
from utils.config import settings


def _compose_occurred_at(budat, cputm) -> str | None:
    if not budat:
        return None
    if cputm:
        dt = datetime.combine(budat, cputm)
        return dt.isoformat()
    return f"{budat}T00:00:00"


def _is_weekend(budat) -> bool:
    return bool(budat and budat.weekday() >= 5)


def _fallback_case_status(header: FiDocHeader) -> str:
    return "NEW"


def _case_id_from_voucher(tenant_id: int, bukrs: str, belnr: str, gjahr: str) -> int:
    """Deterministic case_id for PoC — large integer from MD5 hash."""
    key = f"{tenant_id}:{bukrs}:{belnr}:{gjahr}"
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % (10 ** 14) + 1


def _merchant_name_for_header(header: FiDocHeader) -> str:
    """DEMO/BETA 전표는 시나리오 가맹점명, 그 외는 bktxt/xblnr."""
    xblnr = header.xblnr or ""
    if xblnr.startswith("DEMO-"):
        scenario = xblnr.split("-")[1] if len(xblnr.split("-")) >= 2 else None
        profile = SCENARIO_PROFILES.get(scenario) if scenario else None
        return profile["merchant_name"] if profile else (header.bktxt or xblnr)
    if xblnr.startswith("BETA-"):
        # xblnr 형식: "BETA-{case_type[:8]}-{belnr}"
        parts = xblnr.split("-")
        case_type = parts[1] if len(parts) >= 2 else None
        # case_type이 8자리로 잘렸을 수 있으므로 prefix 매칭
        if case_type:
            matched = next(
                (k for k in SCENARIO_PROFILES if k.startswith(case_type) or case_type.startswith(k[:8])),
                None,
            )
            if matched:
                return SCENARIO_PROFILES[matched]["merchant_name"]
        return header.bktxt or xblnr
    return header.bktxt or header.xblnr or ""


def _scenario_for_header(header: FiDocHeader) -> str | None:
    """DEMO xblnr 형식(DEMO-<SCENARIO>-<SEQ>)에서 시나리오 키를 추출."""
    xblnr = header.xblnr or ""
    if not xblnr.startswith("DEMO-"):
        return None
    raw = xblnr.replace("DEMO-", "")
    return raw.rsplit("-", 1)[0] if "-" in raw else raw


def _build_screening_body(header: FiDocHeader, amount: float | None) -> dict:
    """
    Build body for screening — BE DetectBatchService.buildFlattenedBatchItem와 동일한 핵심 필드.
    BE ScreenBatchItemRequest: occurredAt, hrStatus, hrStatusRaw, mccCode, budgetExceeded, isHoliday (intended_risk_type 미포함).
    """
    is_holiday = _is_weekend(header.budat) or (header.hr_status or "").upper() in {"LEAVE", "OFF", "VACATION"}
    occurred_at = _compose_occurred_at(header.budat, header.cputm)
    hr_raw = header.hr_status
    # BE normalizeHrStatusForAura: WORKING/BUSINESS_TRIP/WORK→WORK, VACATION/OFF/LEAVE→LEAVE
    hr_normalized = (hr_raw or "").upper()
    if hr_normalized in ("WORKING", "BUSINESS_TRIP", "WORK"):
        hr_normalized = "WORK"
    elif hr_normalized in ("VACATION", "OFF", "LEAVE"):
        hr_normalized = "LEAVE"
    return {
        "occurredAt": occurred_at,
        "isHoliday": is_holiday,
        "hrStatus": hr_normalized or hr_raw,
        "hrStatusRaw": hr_raw,
        "mccCode": header.mcc_code,
        "budgetExceeded": (header.budget_exceeded_flag or "").upper() == "Y",
        "amount": float(amount) if amount is not None else 0.0,
        "expenseType": header.blart,
        "merchantName": _merchant_name_for_header(header),
    }


_SCREENING_REQUIRED_FIELDS = (
    "occurredAt",
    "isHoliday",
    "hrStatus",
    "hrStatusRaw",
    "mccCode",
    "budgetExceeded",
    "amount",
)


def _missing_screening_required_fields(screening_body: dict) -> list[str]:
    missing: list[str] = []
    for key in _SCREENING_REQUIRED_FIELDS:
        val = screening_body.get(key)
        if val is None:
            missing.append(key)
            continue
        if isinstance(val, str) and not val.strip():
            missing.append(key)
    return missing


def run_case_screening(
    db: Session,
    voucher_key: str,
    *,
    strict_required_fields: bool = False,
    commit: bool = True,
) -> dict:
    """
    Run screening on a raw voucher and persist the result into AgentCase.
    Returns the screening result dict.
    """
    tenant_id = settings.default_tenant_id
    parts = voucher_key.split("-")
    if len(parts) < 3:
        raise ValueError("invalid voucher_key")
    bukrs, belnr, gjahr = parts[0], parts[1], parts[2]

    header = db.scalar(
        select(FiDocHeader).where(
            FiDocHeader.tenant_id == tenant_id,
            FiDocHeader.bukrs == bukrs,
            FiDocHeader.belnr == belnr,
            FiDocHeader.gjahr == gjahr,
        )
    )
    if not header:
        raise ValueError("voucher not found")

    amount = db.scalar(
        select(func.sum(FiDocItem.wrbtr)).where(
            FiDocItem.tenant_id == tenant_id,
            FiDocItem.bukrs == bukrs,
            FiDocItem.belnr == belnr,
            FiDocItem.gjahr == gjahr,
        )
    )

    screening_body = _build_screening_body(header, amount)
    if strict_required_fields:
        missing = _missing_screening_required_fields(screening_body)
        if missing:
            raise ValueError(
                "screening required field missing: "
                + ",".join(missing)
                + f" (voucher_key={voucher_key})"
            )
    result = run_screening(screening_body)
    screening_meta: dict | None = result.get("screening_meta")

    # Upsert AgentCase with screening result
    existing = db.scalar(
        select(AgentCase).where(
            AgentCase.tenant_id == tenant_id,
            AgentCase.bukrs == bukrs,
            AgentCase.belnr == belnr,
            AgentCase.gjahr == gjahr,
        )
    )
    now = datetime.now(timezone.utc)
    if existing:
        existing.case_type = result["case_type"]
        existing.severity = result["severity"]
        existing.score = result["score"] / 100.0
        existing.reason_text = result["reason_text"]
        existing.status = "NEW"
        # Always overwrite to keep lane/meta aligned with the latest screening run.
        # Fast lane will explicitly clear stale deep metadata with None.
        existing.screening_meta = screening_meta
    else:
        case = AgentCase(
            case_id=_case_id_from_voucher(tenant_id, bukrs, belnr, gjahr),
            tenant_id=tenant_id,
            detected_at=now,
            bukrs=bukrs,
            belnr=belnr,
            gjahr=gjahr,
            buzei="001",
            case_type=result["case_type"],
            severity=result["severity"],
            score=result["score"] / 100.0,
            reason_text=result["reason_text"],
            status="NEW",
            screening_meta=screening_meta,
        )
        db.add(case)
    if commit:
        db.commit()
    else:
        db.flush()

    return {**result, "voucher_key": voucher_key}


def upsert_agent_case_from_screening_result(
    db: Session,
    voucher_key: str,
    *,
    case_type: str,
    severity: str,
    score: float,
    reason_text: str,
    screening_meta: dict | None = None,
) -> None:
    """
    스크리닝 결과만으로 AgentCase를 생성/갱신 (스크리닝 로직 재실행 없음).
    분석 실행 중 screener_node 결과를 DB에 반영할 때 사용.
    screening_meta: Deep lane 결과가 있을 경우 optional로 전달.
    """
    tenant_id = settings.default_tenant_id
    parts = voucher_key.split("-")
    if len(parts) < 3:
        return
    bukrs, belnr, gjahr = parts[0], parts[1], parts[2]

    existing = db.scalar(
        select(AgentCase).where(
            AgentCase.tenant_id == tenant_id,
            AgentCase.bukrs == bukrs,
            AgentCase.belnr == belnr,
            AgentCase.gjahr == gjahr,
        )
    )
    now = datetime.now(timezone.utc)
    if existing:
        existing.case_type = case_type
        existing.severity = severity
        existing.score = score
        existing.reason_text = reason_text
        existing.status = "NEW"
        # Always overwrite to keep lane/meta aligned with the latest screening run.
        # Fast lane will explicitly clear stale deep metadata with None.
        existing.screening_meta = screening_meta
    else:
        case = AgentCase(
            case_id=_case_id_from_voucher(tenant_id, bukrs, belnr, gjahr),
            tenant_id=tenant_id,
            detected_at=now,
            bukrs=bukrs,
            belnr=belnr,
            gjahr=gjahr,
            buzei="001",
            case_type=case_type,
            severity=severity,
            score=score,
            reason_text=reason_text,
            status="NEW",
            screening_meta=screening_meta,
        )
        db.add(case)
    db.commit()


def list_vouchers(db: Session, queue: str = "all", limit: int = 50) -> list[VoucherRow]:
    """
    queue=all: 내 전표 목록 (user_id=1)
    queue=pending: 소명 대기함 (agent_case.status=PENDING_EXPLANATION)
    """
    tenant_id = settings.default_tenant_id
    user_id = settings.default_user_id

    sub_amount = (
        select(
            FiDocItem.tenant_id,
            FiDocItem.bukrs,
            FiDocItem.belnr,
            FiDocItem.gjahr,
            func.sum(FiDocItem.wrbtr).label("amount"),
        )
        .where(FiDocItem.tenant_id == tenant_id)
        .group_by(FiDocItem.tenant_id, FiDocItem.bukrs, FiDocItem.belnr, FiDocItem.gjahr)
        .subquery()
    )

    stmt = (
        select(
            FiDocHeader,
            AgentCase.case_id,
            AgentCase.case_type,
            AgentCase.severity,
            AgentCase.status,
            sub_amount.c.amount,
        )
        .select_from(FiDocHeader)
        .outerjoin(
            AgentCase,
            and_(
                AgentCase.tenant_id == FiDocHeader.tenant_id,
                AgentCase.bukrs == FiDocHeader.bukrs,
                AgentCase.belnr == FiDocHeader.belnr,
                AgentCase.gjahr == FiDocHeader.gjahr,
            ),
        )
        .outerjoin(
            sub_amount,
            and_(
                sub_amount.c.tenant_id == FiDocHeader.tenant_id,
                sub_amount.c.bukrs == FiDocHeader.bukrs,
                sub_amount.c.belnr == FiDocHeader.belnr,
                sub_amount.c.gjahr == FiDocHeader.gjahr,
            ),
        )
        .where(FiDocHeader.tenant_id == tenant_id)
        .where((FiDocHeader.user_id == user_id) | (FiDocHeader.user_id.is_(None)))
    )

    if queue == "pending":
        stmt = stmt.where(AgentCase.status == "PENDING_EXPLANATION")

    stmt = stmt.order_by(desc(FiDocHeader.budat), desc(FiDocHeader.belnr)).limit(limit)
    rows = db.execute(stmt).all()

    out: list[VoucherRow] = []
    for header, case_id, case_type, severity, case_status, amount in rows:
        # case_type is from AgentCase (screened); None = not yet screened
        effective_case_type = case_type or "UNSCREENED"
        effective_case_status = case_status or _fallback_case_status(header)
        effective_severity = severity or "LOW"
        # DEMO 전표: 전표 제목(demo_name)=bktxt, 가맹점명=시나리오 프로필
        xblnr = header.xblnr or ""
        is_demo = xblnr.startswith("DEMO-")
        demo_name = (header.bktxt or xblnr) if is_demo else None
        merchant_name = _merchant_name_for_header(header)
        out.append(
            VoucherRow(
                voucher_key=f"{header.bukrs}-{header.belnr}-{header.gjahr}",
                bukrs=header.bukrs,
                belnr=header.belnr,
                gjahr=header.gjahr,
                amount=float(amount) if amount is not None else None,
                currency=header.waers,
                demo_name=demo_name,
                merchant_name=merchant_name,
                occurred_at=_compose_occurred_at(header.budat, header.cputm),
                hr_status=header.hr_status,
                mcc_code=header.mcc_code,
                budget_exceeded=(header.budget_exceeded_flag or "").upper() == "Y",
                case_id=case_id,
                case_type=effective_case_type,
                severity=effective_severity,
                case_status=effective_case_status,
            )
        )
    return out


def get_agent_case_status(db: Session, voucher_key: str) -> str | None:
    """voucher_key(bukrs-belnr-gjahr)에 해당하는 AgentCase.status 반환. 없으면 None."""
    parts = voucher_key.split("-")
    if len(parts) < 3:
        return None
    bukrs, belnr, gjahr = parts[0], parts[1], parts[2]
    tenant_id = settings.default_tenant_id
    agent_case = db.scalar(
        select(AgentCase.status).where(
            AgentCase.tenant_id == tenant_id,
            AgentCase.bukrs == bukrs,
            AgentCase.belnr == belnr,
            AgentCase.gjahr == gjahr,
        )
    )
    return agent_case


def update_agent_case_status_from_run(db: Session, voucher_key: str, run_status: str | None) -> None:
    """
    Run 결과 상태를 AgentCase.status에 반영해 케이스 목록/KPI 집계가 올바르게 나오도록 함.
    voucher_key는 bukrs-belnr-gjahr 형식.
    """
    if not run_status or not isinstance(run_status, str):
        return
    status = str(run_status).strip().upper()
    # 런타임/도메인 상태를 DB agent_case.status(enum)로 정규화한다.
    # 완료 계열은 RESOLVED, 검토/보류 계열은 IN_REVIEW로 수렴해 enum 제약 오류를 방지한다.
    status_map = {
        "COMPLETED": "RESOLVED",
        "COMPLETED_AFTER_HITL": "RESOLVED",
        "COMPLETED_AFTER_EVIDENCE": "RESOLVED",
        "OK": "RESOLVED",
        "HITL_REQUIRED": "IN_REVIEW",
        "REVIEW_REQUIRED": "IN_REVIEW",
        "REVIEW_AFTER_HITL": "IN_REVIEW",
        "HOLD_AFTER_HITL": "IN_REVIEW",
        "FAILED": "IN_REVIEW",
        "EVIDENCE_REJECTED": "IN_REVIEW",
    }
    status = status_map.get(status, status)
    allowed_db_statuses = {
        "OPEN", "IN_REVIEW", "APPROVED", "REJECTED", "ACTIONED",
        "CLOSED", "TRIAGED", "IN_PROGRESS", "RESOLVED", "DISMISSED",
        "NEW",
    }
    if status not in allowed_db_statuses:
        status = "IN_REVIEW"
    parts = voucher_key.split("-")
    if len(parts) < 3:
        return
    bukrs, belnr, gjahr = parts[0], parts[1], parts[2]
    tenant_id = settings.default_tenant_id
    agent_case = db.scalar(
        select(AgentCase).where(
            AgentCase.tenant_id == tenant_id,
            AgentCase.bukrs == bukrs,
            AgentCase.belnr == belnr,
            AgentCase.gjahr == gjahr,
        )
    )
    if agent_case is not None:
        agent_case.status = status
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise


def build_analysis_payload(db: Session, voucher_key: str) -> dict:
    tenant_id = settings.default_tenant_id
    parts = voucher_key.split("-")
    if len(parts) < 3:
        raise ValueError("invalid voucher_key")
    bukrs, belnr, gjahr = parts[0], parts[1], parts[2]

    header = db.scalar(
        select(FiDocHeader).where(
            FiDocHeader.tenant_id == tenant_id,
            FiDocHeader.bukrs == bukrs,
            FiDocHeader.belnr == belnr,
            FiDocHeader.gjahr == gjahr,
        )
    )
    if not header:
        raise ValueError("voucher not found")

    items = db.scalars(
        select(FiDocItem).where(
            FiDocItem.tenant_id == tenant_id,
            FiDocItem.bukrs == bukrs,
            FiDocItem.belnr == belnr,
            FiDocItem.gjahr == gjahr,
        ).order_by(FiDocItem.buzei)
    ).all()
    item = items[0] if items else None

    amount = db.scalar(
        select(func.sum(FiDocItem.wrbtr)).where(
            FiDocItem.tenant_id == tenant_id,
            FiDocItem.bukrs == bukrs,
            FiDocItem.belnr == belnr,
            FiDocItem.gjahr == gjahr,
        )
    )

    occurred_at = _compose_occurred_at(header.budat, header.cputm)
    is_holiday = _is_weekend(header.budat) or (header.hr_status or "").upper() in {"LEAVE", "OFF", "VACATION"}

    # Prefer AgentCase screened case_type over pre-labeled intended_risk_type.
    # If no screening has run yet, leave case_type as None so the screener_node
    # in LangGraph will classify it from raw signals during analysis.
    agent_case = db.scalar(
        select(AgentCase).where(
            AgentCase.tenant_id == tenant_id,
            AgentCase.bukrs == bukrs,
            AgentCase.belnr == belnr,
            AgentCase.gjahr == gjahr,
        )
    )
    screened_case_type: str | None = agent_case.case_type if agent_case else None
    screened_severity: str | None = agent_case.severity if agent_case else None
    screened_score: float | None = float(agent_case.score) if agent_case and agent_case.score else None

    # case_type passed to agent — 스크리닝 결과(AgentCase)가 있으면 그대로 사용, 없으면 None(분석 시 screener_node가 분류)
    case_type = screened_case_type

    # BE CaseAnalysisService.buildEvidenceSnapshot와 동일: 스크리닝 결과(case_type, screening_reason_text) + 전표/evidence 필드
    # → 분석 버튼 시 에이전트에 넘기는 값 = 이 body_evidence (기존 소스와 동일 구조)
    document_items = [
        {
            "buzei": it.buzei,
            "hkont": it.hkont,
            "wrbtr": float(it.wrbtr) if it.wrbtr is not None else None,
            "waers": it.waers,
            "lifnr": it.lifnr,
            "sgtxt": it.sgtxt,
            "source": "fi_doc_item",
        }
        for it in items
    ]

    scenario = _scenario_for_header(header)
    is_normal_demo = scenario == "NORMAL_BASELINE"

    body_evidence = {
        "doc_id": f"{bukrs}-{belnr}-{gjahr}",
        "item_id": item.buzei if item else "001",
        # case_type is None when not screened yet — screener_node will classify during analysis
        "case_type": case_type,
        "screening_reason_text": agent_case.reason_text if agent_case else None,
        "screening_score": screened_score,
        "severity": screened_severity,
        "occurredAt": occurred_at,
        "amount": float(amount) if amount is not None else None,
        "expenseType": header.blart,
        "merchantName": _merchant_name_for_header(header),
        "hrStatus": header.hr_status,
        "hrStatusRaw": header.hr_status,
        "mccCode": header.mcc_code,
        "budgetExceeded": (header.budget_exceeded_flag or "").upper() == "Y",
        "intended_risk_type": case_type,
        "bukrs": bukrs,
        "belnr": belnr,
        "gjahr": gjahr,
        "buzei": item.buzei if item else "001",
        "isHoliday": is_holiday,
        "document": {
            "type": "DOCUMENT",
            "docKey": f"{bukrs}-{belnr}-{gjahr}",
            "header": {
                "budat": str(header.budat) if header.budat else None,
                "waers": header.waers,
                "blart": header.blart,
            },
            "items": document_items,
        },
        "dataQuality": {
            "missingFields": [
                key for key, value in {
                    "occurredAt": occurred_at,
                    "amount": amount,
                    "expenseType": header.blart,
                    "merchantName": _merchant_name_for_header(header),
                    "hrStatus": header.hr_status,
                    "mccCode": header.mcc_code,
                }.items() if value in (None, "")
            ],
        },
    }

    # 정상 케이스 테스트 데이터는 규정 "필수 입력/증빙" 항목을 사전 충족 상태로 채워 전달한다.
    # (업무 목적, 참석자(내부/외부), 일시/장소, 결제수단, 적격 증빙 여부)
    if is_normal_demo:
        attendees = [
            {"name": "내부참석자A", "type": "INTERNAL", "org": "재무팀"},
            {"name": "내부참석자B", "type": "INTERNAL", "org": "재무팀"},
            {"name": "외부참석자C", "type": "EXTERNAL", "org": "거래처A"},
        ]
        body_evidence.update(
            {
                "businessPurpose": "업무 협의 식대",
                "attendees": attendees,
                "attendeeCount": len(attendees),
                "location": "서울 본사 인근 식당",
                "paymentMethod": "법인카드",
                "evidenceProvided": True,
            }
        )
        document = body_evidence.get("document") or {}
        document["businessPurpose"] = body_evidence["businessPurpose"]
        document["attendees"] = attendees
        document["attendeeCount"] = len(attendees)
        document["location"] = body_evidence["location"]
        document["paymentMethod"] = body_evidence["paymentMethod"]
        document["receiptQualified"] = True
        body_evidence["document"] = document

    case_id_int = _case_id_from_voucher(tenant_id, bukrs, belnr, gjahr)
    return {
        "case_id": f"POC-{bukrs}-{belnr}-{gjahr}",
        "case_id_int": case_id_int,
        "body_evidence": body_evidence,
        "intended_risk_type": case_type,
        "document_items": document_items,
    }
