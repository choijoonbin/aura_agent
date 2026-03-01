from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from db.models import AgentCase, FiDocHeader, FiDocItem
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


def _fallback_case_type(header: FiDocHeader) -> str:
    return header.intended_risk_type or "NORMAL_BASELINE"


def _fallback_case_status(header: FiDocHeader) -> str:
    return "NEW"


def _fallback_severity(header: FiDocHeader) -> str:
    risk = (header.intended_risk_type or "").upper()
    if risk in {"HOLIDAY_USAGE", "LIMIT_EXCEED", "PRIVATE_USE_RISK"}:
        return "HIGH" if (header.budget_exceeded_flag or "").upper() == "Y" else "MEDIUM"
    if risk in {"UNUSUAL_PATTERN", "SPLIT_PAYMENT", "DUPLICATE_SUSPECT"}:
        return "MEDIUM"
    return "LOW"


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
        effective_case_type = case_type or _fallback_case_type(header)
        effective_case_status = case_status or _fallback_case_status(header)
        effective_severity = severity or _fallback_severity(header)
        out.append(
            VoucherRow(
                voucher_key=f"{header.bukrs}-{header.belnr}-{header.gjahr}",
                bukrs=header.bukrs,
                belnr=header.belnr,
                gjahr=header.gjahr,
                amount=float(amount) if amount is not None else None,
                currency=header.waers,
                merchant_name=header.bktxt or header.xblnr,
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
    case_type = header.intended_risk_type or "HOLIDAY_USAGE"
    is_holiday = _is_weekend(header.budat) or (header.hr_status or "").upper() in {"LEAVE", "OFF", "VACATION"}

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

    body_evidence = {
        "doc_id": f"{bukrs}-{belnr}-{gjahr}",
        "item_id": item.buzei if item else "001",
        "case_type": case_type,
        "screening_reason_text": "PoC screening result",
        "occurredAt": occurred_at,
        "amount": float(amount) if amount is not None else None,
        "expenseType": header.blart,
        "merchantName": header.bktxt or header.xblnr,
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
                    "merchantName": header.bktxt or header.xblnr,
                    "hrStatus": header.hr_status,
                    "mccCode": header.mcc_code,
                }.items() if value in (None, "")
            ],
        },
    }

    return {
        "case_id": f"POC-{bukrs}-{belnr}-{gjahr}",
        "body_evidence": body_evidence,
        "intended_risk_type": case_type,
        "document_items": document_items,
    }
