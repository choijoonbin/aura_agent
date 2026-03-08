"""
전표–증빙 비교: 추출된 증빙 필드와 body_evidence(전표)를 정량 규칙으로 비교.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from services.evidence_extraction import ExtractedEvidence

# 정책값 (문서 2.4)
AMOUNT_ABS_TOLERANCE = 100  # 원
AMOUNT_REL_TOLERANCE = 0.005  # 0.5%
DATE_TOLERANCE_DAYS = 3  # ±N일


@dataclass
class ComparisonResult:
    passed: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    comparison_detail: dict[str, Any] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _days_diff(d1: datetime | None, d2: datetime | None) -> int | None:
    if d1 is None or d2 is None:
        return None
    delta = (d1.replace(tzinfo=timezone.utc) - d2.replace(tzinfo=timezone.utc)).days
    return abs(delta)


def compare_evidence_to_voucher(
    extracted: ExtractedEvidence,
    body_evidence: dict[str, Any],
) -> ComparisonResult:
    """
    추출된 증빙과 전표(body_evidence)를 비교하여 통과 여부와 사유 반환.
    """
    reasons: list[str] = []
    mismatches: list[str] = []
    detail: dict[str, Any] = {}
    voucher_amount = body_evidence.get("amount")
    if voucher_amount is not None:
        try:
            voucher_amount = float(voucher_amount)
        except (TypeError, ValueError):
            voucher_amount = None
    occurred_at = body_evidence.get("occurredAt")
    voucher_date = _parse_date(occurred_at[:10] if isinstance(occurred_at, str) and len(occurred_at) >= 10 else None)
    mcc_code = (body_evidence.get("mccCode") or "").strip() or None
    merchant_name = (body_evidence.get("merchantName") or "").strip() or None

    # 금액 비교
    if extracted.amount is not None and voucher_amount is not None:
        abs_diff = abs(extracted.amount - voucher_amount)
        rel_diff = abs_diff / voucher_amount if voucher_amount else 0
        detail["amount"] = {"voucher": voucher_amount, "extracted": extracted.amount, "abs_diff": abs_diff, "rel_diff": rel_diff}
        if abs_diff <= AMOUNT_ABS_TOLERANCE or rel_diff <= AMOUNT_REL_TOLERANCE:
            reasons.append("금액 일치")
        else:
            mismatches.append("amount")
            reasons.append(f"금액 불일치: 전표={voucher_amount}, 증빙={extracted.amount}")
    elif extracted.amount is None and voucher_amount is not None:
        mismatches.append("amount")
        reasons.append("증빙에서 금액 미추출")
    else:
        reasons.append("금액 비교 생략(데이터 없음)")

    # 날짜 비교 (approval_date vs occurred_at)
    ev_date = _parse_date(extracted.approval_date)
    if ev_date is not None and voucher_date is not None:
        days = _days_diff(ev_date, voucher_date)
        detail["date"] = {"voucher": str(voucher_date.date()), "extracted": extracted.approval_date, "days_diff": days}
        if days is not None and days <= DATE_TOLERANCE_DAYS:
            reasons.append("승인일자 일치")
        else:
            mismatches.append("date")
            reasons.append(f"날짜 불일치: 전표={voucher_date.date()}, 증빙={extracted.approval_date}")
    elif ev_date is None and voucher_date is not None:
        mismatches.append("date")
        reasons.append("증빙에서 승인일자 미추출")
    else:
        reasons.append("날짜 비교 생략(데이터 없음)")

    # 업종 비교 (mcc 또는 업종명)
    if extracted.industry_or_mcc or mcc_code:
        ext_mcc = (extracted.industry_or_mcc or "").strip()
        detail["mcc"] = {"voucher": mcc_code, "extracted": ext_mcc}
        if mcc_code and ext_mcc and (mcc_code == ext_mcc or ext_mcc in (mcc_code, merchant_name or "")):
            reasons.append("업종/MCC 일치")
        elif not ext_mcc:
            mismatches.append("mcc")
            reasons.append("증빙에서 업종/MCC 미추출")
        else:
            mismatches.append("mcc")
            reasons.append(f"업종 불일치: 전표={mcc_code}, 증빙={ext_mcc}")
    else:
        reasons.append("업종 비교 생략")

    passed = len(mismatches) == 0
    confidence = extracted.confidence if passed else max(0.0, extracted.confidence - 0.3)
    return ComparisonResult(
        passed=passed,
        confidence=confidence,
        reasons=reasons,
        extracted_fields={
            "amount": extracted.amount,
            "approval_date": extracted.approval_date,
            "industry_or_mcc": extracted.industry_or_mcc,
            "merchant_name": extracted.merchant_name,
        },
        comparison_detail=detail,
        mismatches=mismatches,
    )
