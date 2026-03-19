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
TIME_TOLERANCE_MINUTES = 60  # ±N분


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


def _parse_time_to_minutes(s: str | None) -> int | None:
    if not s:
        return None
    raw = str(s).strip()
    # HH:MM(:SS) 또는 occurredAt(YYYY-MM-DDTHH:MM:SS)
    if len(raw) >= 16 and ("T" in raw or " " in raw):
        raw = raw[11:16]
    if len(raw) < 5 or ":" not in raw:
        return None
    try:
        hh, mm = raw[:5].split(":")
        hhi = int(hh)
        mmi = int(mm)
        if not (0 <= hhi <= 23 and 0 <= mmi <= 59):
            return None
        return hhi * 60 + mmi
    except Exception:
        return None


def _minutes_diff(t1: int | None, t2: int | None) -> int | None:
    if t1 is None or t2 is None:
        return None
    return abs(t1 - t2)


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
    occurred_minutes = _parse_time_to_minutes(occurred_at if isinstance(occurred_at, str) else None)

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

    # 시간 비교 (approval_time vs occurred_at의 HH:MM)
    ev_minutes = _parse_time_to_minutes(extracted.approval_time)
    if ev_minutes is not None and occurred_minutes is not None:
        mins = _minutes_diff(ev_minutes, occurred_minutes)
        detail["time"] = {
            "voucher": occurred_at[11:16] if isinstance(occurred_at, str) and len(occurred_at) >= 16 else None,
            "extracted": extracted.approval_time,
            "minutes_diff": mins,
        }
        if mins is not None and mins <= TIME_TOLERANCE_MINUTES:
            reasons.append("승인시간 일치")
        else:
            mismatches.append("time")
            reasons.append(
                f"시간 불일치: 전표={detail['time']['voucher']}, 증빙={extracted.approval_time}"
            )
    elif ev_minutes is None and occurred_minutes is not None:
        mismatches.append("time")
        reasons.append("증빙에서 승인시간 미추출")
    else:
        reasons.append("시간 비교 생략(데이터 없음)")

    # 업종/MCC 비교는 현재 판정 대상에서 제외 (UI 노출 문구는 추가하지 않음)

    passed = len(mismatches) == 0
    confidence = extracted.confidence if passed else max(0.0, extracted.confidence - 0.3)
    return ComparisonResult(
        passed=passed,
        confidence=confidence,
        reasons=reasons,
        extracted_fields={
            "amount": extracted.amount,
            "approval_date": extracted.approval_date,
            "approval_time": extracted.approval_time,
            "industry_or_mcc": extracted.industry_or_mcc,
            "merchant_name": extracted.merchant_name,
        },
        comparison_detail=detail,
        mismatches=mismatches,
    )
