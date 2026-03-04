from __future__ import annotations

import random
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from db.models import AgentCase, FiDocHeader, FiDocItem
from utils.config import settings


# BE(dwp-backend) DemoViolationService 시나리오와 동일한 필드 규칙 적용.
# BE: setContextForScenario → hr_status, mcc_code, budget_exceeded_flag
#     preferredMccCodes(HOLIDAY)=5813,5812,5814 / LIMIT=7011,4722,5812 / PRIVATE=7992,5813,7011 / UNUSUAL=4722,7011,7992,5813 / DEFAULT=5812,5814
#     resolveBudgetExceededFlag: SPLIT_PAYMENT,LIMIT_EXCEED,OVER_LIMIT→Y / UNUSUAL→random / else N
#     HOLIDAY_USAGE는 budget N (휴일 사용 의심은 한도초과와 별개).
# BE screen-batch 요청에는 intended_risk_type 미포함 → Aura가 전표 원시 데이터만으로 스크리닝.
SCENARIO_PROFILES: dict[str, dict[str, Any]] = {
    "HOLIDAY_USAGE": {
        "label": "휴일 사용 의심",
        "description": "주말/휴무일 심야 식대 사용 시나리오",
        "blart": "SA",
        "hr_status": "LEAVE",  # BE: weekend→OFF, calendar or fallback
        "mcc_code": "5813",   # BE preferred: 5813, 5812, 5814
        "budget_flag": "N",   # BE: HOLIDAY_USAGE는 N
        "merchant_name": "심야 식대",
        "item_text": "휴일 야간 식대",
        "amount_range": (30000, 150000),
        "hour_candidates": [22, 23, 1],
        "day_mode": "weekend",
        "belnr_prefix": "H",
        "risk_type": "HOLIDAY_USAGE",
    },
    "LIMIT_EXCEED": {
        "label": "한도 초과 의심",
        "description": "높은 금액의 접대/업무 식대 한도 초과 시나리오",
        "blart": "SA",
        "hr_status": "WORK",
        "mcc_code": "5812",   # BE preferred: 7011, 4722, 5812
        "budget_flag": "Y",   # BE: LIMIT_EXCEED→Y
        "merchant_name": "고액 식대",
        "item_text": "고액 접대비",
        "amount_range": (250000, 900000),
        "hour_candidates": [12, 12, 13],  # BE: 12:xx
        "day_mode": "weekday",
        "belnr_prefix": "L",
        "risk_type": "LIMIT_EXCEED",
    },
    "PRIVATE_USE_RISK": {
        "label": "사적 사용 위험",
        "description": "업무 외 목적이 의심되는 개인성 지출 시나리오",
        "blart": "SA",
        "hr_status": "LEAVE",
        "mcc_code": "7992",   # BE preferred: 7992, 5813, 7011
        "budget_flag": "N",
        "merchant_name": "레저 시설",
        "item_text": "개인성 레저 지출",
        "amount_range": (80000, 250000),
        "hour_candidates": [13, 14, 15],
        "day_mode": "weekend",
        "belnr_prefix": "P",
        "risk_type": "PRIVATE_USE_RISK",
    },
    "UNUSUAL_PATTERN": {
        "label": "비정상 패턴",
        "description": "평소와 다른 시간대/업종/금액 조합 시나리오",
        "blart": "SA",
        "hr_status": "WORK",
        "mcc_code": "5813",   # BE preferred: 4722, 7011, 7992, 5813
        "budget_flag": "N",   # BE: random Y/N — PoC에서는 N 고정 가능
        "merchant_name": "심야 간편식",
        "item_text": "비정상 패턴 식대",
        "amount_range": (120000, 300000),
        "hour_candidates": [0, 2, 3],
        "day_mode": "weekday",
        "belnr_prefix": "U",
        "risk_type": "UNUSUAL_PATTERN",
    },
    "NORMAL_BASELINE": {
        "label": "정상 비교군",
        "description": "정상 업무 시간대의 일반 식대 시나리오 (BE DEFAULT/NORMAL)",
        "blart": "SA",
        "hr_status": "WORK",
        "mcc_code": "5812",   # BE preferred: 5812, 5814
        "budget_flag": "N",
        "merchant_name": "일반 식대",
        "item_text": "정상 업무 식대",
        "amount_range": (12000, 45000),
        "hour_candidates": [11, 12, 13],
        "day_mode": "weekday",
        "belnr_prefix": "N",
        "risk_type": "NORMAL_BASELINE",
    },
}


def list_demo_scenarios() -> list[dict[str, Any]]:
    out = []
    for key, profile in SCENARIO_PROFILES.items():
        out.append(
            {
                "scenario": key,
                "label": profile["label"],
                "description": profile["description"],
                "amount_range": profile["amount_range"],
                "day_mode": profile["day_mode"],
                "risk_type": profile["risk_type"],
            }
        )
    return out


def _next_day(mode: str) -> date:
    target = date.today()
    if mode == "weekend":
        while target.weekday() != 5:
            target = date.fromordinal(target.toordinal() + 1)
        return target
    while target.weekday() >= 5:
        target = date.fromordinal(target.toordinal() + 1)
    return target


def seed_demo_scenarios(db: Session, scenario: str, count: int = 5) -> dict[str, Any]:
    tenant_id = settings.default_tenant_id
    user_id = settings.default_user_id
    profile = SCENARIO_PROFILES.get(scenario)
    if not profile:
        raise ValueError(f"unsupported scenario: {scenario}")

    target_day = _next_day(profile["day_mode"])
    inserted = 0
    keys: list[str] = []
    prefix = profile["belnr_prefix"]
    gjahr_str = str(target_day.year)

    # Find next available index so repeated "생성" clicks always add new records
    existing_count = db.scalar(
        select(func.count(FiDocHeader.belnr)).where(
            FiDocHeader.tenant_id == tenant_id,
            FiDocHeader.bukrs == "1000",
            FiDocHeader.belnr.like(f"{prefix}%"),
            FiDocHeader.gjahr == gjahr_str,
        )
    ) or 0
    start_index = existing_count + 1

    for i in range(count):
        belnr = f"{prefix}{start_index + i:09d}"[-10:]
        exists = db.scalar(
            select(FiDocHeader).where(
                FiDocHeader.tenant_id == tenant_id,
                FiDocHeader.bukrs == "1000",
                FiDocHeader.belnr == belnr,
                FiDocHeader.gjahr == gjahr_str,
            )
        )
        if exists:
            continue

        seq = start_index + i
        header = FiDocHeader(
            tenant_id=tenant_id,
            bukrs="1000",
            belnr=belnr,
            gjahr=gjahr_str,
            user_id=user_id,
            doc_source="POC",
            budat=target_day,
            cpudt=target_day,
            cputm=time(
                hour=random.choice(profile["hour_candidates"]),
                minute=random.randint(0, 59),
                second=random.randint(0, 59),
            ),
            blart=profile["blart"],
            waers="KRW",
            bktxt=f"POC {profile['merchant_name']} {seq}",
            xblnr=f"DEMO-{scenario}-{seq}",
            intended_risk_type=None,  # Raw data: screener_node will classify during analysis
            hr_status=profile["hr_status"],
            mcc_code=profile["mcc_code"],
            budget_exceeded_flag=profile["budget_flag"],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            created_by=user_id,
            updated_by=user_id,
        )
        item = FiDocItem(
            tenant_id=tenant_id,
            bukrs="1000",
            belnr=belnr,
            gjahr=gjahr_str,
            buzei="001",
            hkont="0000601000",
            wrbtr=random.randint(*profile["amount_range"]),
            waers="KRW",
            lifnr=f"C{1000+i}",
            sgtxt=profile["item_text"],
        )
        db.add(header)
        db.add(item)
        inserted += 1
        keys.append(f"1000-{belnr}-{gjahr_str}")

    db.commit()
    return {"scenario": scenario, "inserted": inserted, "voucher_keys": keys}


def list_seeded_demo_cases(db: Session) -> list[dict[str, Any]]:
    tenant_id = settings.default_tenant_id
    amount_sub = (
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
        select(FiDocHeader, amount_sub.c.amount, AgentCase.case_type, AgentCase.status)
        .select_from(FiDocHeader)
        .outerjoin(
            amount_sub,
            and_(
                amount_sub.c.tenant_id == FiDocHeader.tenant_id,
                amount_sub.c.bukrs == FiDocHeader.bukrs,
                amount_sub.c.belnr == FiDocHeader.belnr,
                amount_sub.c.gjahr == FiDocHeader.gjahr,
            ),
        )
        .outerjoin(
            AgentCase,
            and_(
                AgentCase.tenant_id == FiDocHeader.tenant_id,
                AgentCase.bukrs == FiDocHeader.bukrs,
                AgentCase.belnr == FiDocHeader.belnr,
                AgentCase.gjahr == FiDocHeader.gjahr,
            ),
        )
        .where(
            FiDocHeader.tenant_id == tenant_id,
            FiDocHeader.xblnr.like("DEMO-%"),
        )
        .order_by(FiDocHeader.created_at.desc(), FiDocHeader.belnr.desc())
    )
    rows = db.execute(stmt).all()
    out: list[dict[str, Any]] = []
    for header, amount, ac_case_type, ac_status in rows:
        # 스크리닝/분석 후 갱신된 case_type·case_status 반영 (없으면 시나리오 risk_type·신규)
        case_type = ac_case_type if ac_case_type else (header.intended_risk_type or "UNSCREENED")
        case_status = ac_status if ac_status else "NEW"
        out.append(
            {
                "voucher_key": f"{header.bukrs}-{header.belnr}-{header.gjahr}",
                "scenario": (header.xblnr or "").replace("DEMO-", "").split("-")[0] if header.xblnr else "-",
                "title": header.bktxt or header.xblnr,
                "amount": float(amount) if amount is not None else None,
                "currency": header.waers,
                "risk_type": header.intended_risk_type,
                "case_type": case_type,
                "case_status": case_status,
                "hr_status": header.hr_status,
                "mcc_code": header.mcc_code,
                "budget_exceeded": (header.budget_exceeded_flag or "").upper() == "Y",
                "created_at": header.created_at.isoformat() if header.created_at else None,
            }
        )
    return out


def clear_demo_data(db: Session) -> dict[str, Any]:
    tenant_id = settings.default_tenant_id
    headers = db.scalars(
        select(FiDocHeader).where(
            FiDocHeader.tenant_id == tenant_id,
            FiDocHeader.xblnr.like("DEMO-%"),
        )
    ).all()
    count = 0
    for h in headers:
        items = db.scalars(
            select(FiDocItem).where(
                FiDocItem.tenant_id == h.tenant_id,
                FiDocItem.bukrs == h.bukrs,
                FiDocItem.belnr == h.belnr,
                FiDocItem.gjahr == h.gjahr,
            )
        ).all()
        for it in items:
            db.delete(it)
        db.delete(h)
        count += 1
    db.commit()
    return {"deleted": count}
