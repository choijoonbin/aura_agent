from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import and_, delete, func, select, text
from sqlalchemy.orm import Session

from db.models import AgentCase, FiDocHeader, FiDocItem
from services.stream_runtime import runtime
from utils.config import settings


# 시연 데이터 규칙:
# - AuraAgent 표준 case_type 5종(HOLIDAY_USAGE/LIMIT_EXCEED/PRIVATE_USE_RISK/UNUSUAL_PATTERN/NORMAL_BASELINE)만 사용.
# - 스크리닝 핵심 입력 필드(occurredAt, isHoliday, hrStatus, hrStatusRaw, mccCode, budgetExceeded, amount)를
#   생성 단계에서 모두 채우도록 시나리오 프로파일을 강제 검증한다.
# - 정상 비교군(NORMAL_BASELINE)은 분석 시 HITL 없이 완료되도록 규정에 걸리지 않는 값으로 고정:
#   평일(day_mode=weekday), 낮 시간(hour_candidates=11~13), hr_status=WORK, mcc_code=5816(위험 MCC 아님), budget=N.
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
        "amount_range": (30000, 99999),
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
        # 규정 미걸림: 5816(케이터링/급식) — MCC_HIGH/MEDIUM/LEISURE 기본값에 없음. 심야·휴일 규정 회피용.
        "mcc_code": "5816",
        "budget_flag": "N",
        "merchant_name": "일반 식대",
        "item_text": "정상 업무 식대",
        "amount_range": (12000, 45000),
        # 낮 시간만 사용(심야 제한 규정 회피). 11~13시 = 업무 시간대.
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


def _eul_reul(label: str) -> str:
    """한글 조사: 받침 있으면 '을', 없으면 '를'."""
    if not label or not label.strip():
        return "을"
    last = label.strip()[-1]
    if "\uAC00" <= last <= "\uD7A3":
        return "을" if (ord(last) - 0xAC00) % 28 != 0 else "를"
    return "을"


def _next_day(mode: str) -> date:
    target = date.today()
    if mode == "weekend":
        while target.weekday() != 5:
            target = date.fromordinal(target.toordinal() + 1)
        return target
    while target.weekday() >= 5:
        target = date.fromordinal(target.toordinal() + 1)
    return target


def _case_id_from_voucher(tenant_id: int, bukrs: str, belnr: str, gjahr: str) -> int:
    """Deterministic case_id for PoC — must match services.case_service."""
    key = f"{tenant_id}:{bukrs}:{belnr}:{gjahr}"
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % (10 ** 14) + 1


def _validate_scenario_profile(scenario: str, profile: dict[str, Any]) -> None:
    required = (
        "hr_status",
        "mcc_code",
        "budget_flag",
        "amount_range",
        "hour_candidates",
        "day_mode",
    )
    missing = [k for k in required if k not in profile]
    if missing:
        raise ValueError(f"invalid demo scenario profile({scenario}): missing={','.join(missing)}")

    if str(profile["day_mode"]) not in {"weekday", "weekend"}:
        raise ValueError(f"invalid day_mode in scenario({scenario}): {profile['day_mode']}")
    if str(profile["budget_flag"]).upper() not in {"Y", "N"}:
        raise ValueError(f"invalid budget_flag in scenario({scenario}): {profile['budget_flag']}")
    if not str(profile["mcc_code"]).strip():
        raise ValueError(f"invalid mcc_code in scenario({scenario})")
    if not str(profile["hr_status"]).strip():
        raise ValueError(f"invalid hr_status in scenario({scenario})")

    hours = profile["hour_candidates"]
    if not isinstance(hours, list) or not hours:
        raise ValueError(f"invalid hour_candidates in scenario({scenario})")
    for h in hours:
        if not isinstance(h, int) or h < 0 or h > 23:
            raise ValueError(f"invalid hour value in scenario({scenario}): {h}")
    # 정상 비교군: 규정(심야·휴일 등)에 걸리지 않도록 낮 시간(6~21)·평일만 허용
    if scenario == "NORMAL_BASELINE":
        if profile["day_mode"] != "weekday":
            raise ValueError(f"NORMAL_BASELINE must use day_mode=weekday (current: {profile['day_mode']})")
        if any(h < 6 or h > 21 for h in hours):
            raise ValueError(f"NORMAL_BASELINE hour_candidates must be daytime 6~21 to avoid night rules (current: {hours})")

    amount_range = profile["amount_range"]
    if (
        not isinstance(amount_range, tuple)
        or len(amount_range) != 2
        or not isinstance(amount_range[0], int)
        or not isinstance(amount_range[1], int)
        or amount_range[0] <= 0
        or amount_range[1] < amount_range[0]
    ):
        raise ValueError(f"invalid amount_range in scenario({scenario}): {amount_range}")


def seed_demo_scenarios(db: Session, scenario: str, count: int = 5) -> dict[str, Any]:
    tenant_id = settings.default_tenant_id
    user_id = settings.default_user_id
    profile = SCENARIO_PROFILES.get(scenario)
    if not profile:
        raise ValueError(f"unsupported scenario: {scenario}")
    _validate_scenario_profile(scenario, profile)

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
            bktxt=f"{profile['label']}{_eul_reul(profile['label'])} 위한 테스트 데이터",
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

    # 테스트 데이터 생성 시점에 스크리닝까지 수행:
    # - strict_required_fields=True로 핵심 필드 누락 시 즉시 실패
    # - commit=False로 개별 커밋을 막고 일괄 커밋(원자성 유지)
    from services.case_service import run_case_screening

    try:
        if keys:
            db.flush()
            for vkey in keys:
                run_case_screening(
                    db,
                    vkey,
                    strict_required_fields=True,
                    commit=False,
                )
        db.commit()
    except Exception:
        db.rollback()
        raise

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
        scenario = (header.xblnr or "").replace("DEMO-", "").split("-")[0] if header.xblnr else "-"
        profile = SCENARIO_PROFILES.get(scenario) if scenario else None
        out.append(
            {
                "voucher_key": f"{header.bukrs}-{header.belnr}-{header.gjahr}",
                "scenario": scenario,
                "title": header.bktxt or header.xblnr,
                "merchant_name": profile["merchant_name"] if profile else (header.bktxt or "-"),
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

    if not headers:
        return {
            "deleted": 0,
            "fi_doc_header_deleted": 0,
            "fi_doc_item_deleted": 0,
            "agent_case_deleted": 0,
            "agent_activity_log_deleted": 0,
            "case_analysis_result_deleted": 0,
            "case_analysis_run_deleted": 0,
            "thought_chain_log_deleted": 0,
            "case_action_proposal_deleted": 0,
            "case_action_execution_deleted": 0,
            "run_count": 0,
        }

    voucher_keys: list[str] = []
    case_ids_int: set[int] = set()
    case_ids_str: set[str] = set()
    for h in headers:
        vkey = f"{h.bukrs}-{h.belnr}-{h.gjahr}"
        voucher_keys.append(vkey)
        case_ids_str.add(f"POC-{vkey}")
        case_ids_int.add(_case_id_from_voucher(tenant_id, h.bukrs, h.belnr, h.gjahr))

    run_ids: set[str] = set()
    for case_id_int in case_ids_int:
        rows = db.execute(
            text(
                """
                select run_id::text
                from dwp_aura.case_analysis_run
                where tenant_id = :tenant_id
                  and case_id = :case_id
                """
            ),
            {"tenant_id": tenant_id, "case_id": case_id_int},
        ).scalars().all()
        run_ids.update(str(r) for r in rows if r)

    for case_id in case_ids_str:
        rows = db.execute(
            text(
                """
                select distinct resource_id
                from dwp_aura.agent_activity_log
                where tenant_id = :tenant_id
                  and resource_type = 'analysis_run'
                  and metadata_json ->> 'case_id' = :case_id
                """
            ),
            {"tenant_id": tenant_id, "case_id": case_id},
        ).scalars().all()
        run_ids.update(str(r) for r in rows if r)

    for voucher_key in voucher_keys:
        rows = db.execute(
            text(
                """
                select distinct resource_id
                from dwp_aura.agent_activity_log
                where tenant_id = :tenant_id
                  and resource_type = 'analysis_run'
                  and metadata_json ->> 'voucher_key' = :voucher_key
                """
            ),
            {"tenant_id": tenant_id, "voucher_key": voucher_key},
        ).scalars().all()
        run_ids.update(str(r) for r in rows if r)

    deleted = {
        "fi_doc_header_deleted": 0,
        "fi_doc_item_deleted": 0,
        "agent_case_deleted": 0,
        "agent_activity_log_deleted": 0,
        "case_analysis_result_deleted": 0,
        "case_analysis_run_deleted": 0,
        "thought_chain_log_deleted": 0,
        "case_action_proposal_deleted": 0,
        "case_action_execution_deleted": 0,
    }

    def _table_exists(table_name: str) -> bool:
        row = db.execute(
            text(
                """
                select 1
                from information_schema.tables
                where table_schema = 'dwp_aura'
                  and table_name = :table_name
                limit 1
                """
            ),
            {"table_name": table_name},
        ).scalar_one_or_none()
        return row is not None

    def _table_has_run_id_column(table_name: str) -> bool:
        row = db.execute(
            text(
                """
                select 1
                from information_schema.columns
                where table_schema = 'dwp_aura'
                  and table_name = :table_name
                  and column_name = 'run_id'
                limit 1
                """
            ),
            {"table_name": table_name},
        ).scalar_one_or_none()
        return row is not None

    # Optional run-linked tables (if present in current schema).
    optional_run_tables = {
        "thought_chain_log": "thought_chain_log_deleted",
        "case_action_proposal": "case_action_proposal_deleted",
        "case_action_execution": "case_action_execution_deleted",
    }
    for table_name, counter_key in optional_run_tables.items():
        if not _table_exists(table_name) or not _table_has_run_id_column(table_name):
            continue
        for run_id in run_ids:
            rc = db.execute(
                text(f"delete from dwp_aura.{table_name} where run_id::text = :run_id"),
                {"run_id": run_id},
            ).rowcount or 0
            deleted[counter_key] += int(rc)

    for run_id in run_ids:
        rc = db.execute(
            text(
                """
                delete from dwp_aura.agent_activity_log
                where tenant_id = :tenant_id
                  and resource_type = 'analysis_run'
                  and resource_id = :run_id
                """
            ),
            {"tenant_id": tenant_id, "run_id": run_id},
        ).rowcount or 0
        deleted["agent_activity_log_deleted"] += int(rc)

        rc = db.execute(
            text(
                """
                delete from dwp_aura.case_analysis_result
                where tenant_id = :tenant_id
                  and run_id::text = :run_id
                """
            ),
            {"tenant_id": tenant_id, "run_id": run_id},
        ).rowcount or 0
        deleted["case_analysis_result_deleted"] += int(rc)

        rc = db.execute(
            text(
                """
                delete from dwp_aura.case_analysis_run
                where tenant_id = :tenant_id
                  and run_id::text = :run_id
                """
            ),
            {"tenant_id": tenant_id, "run_id": run_id},
        ).rowcount or 0
        deleted["case_analysis_run_deleted"] += int(rc)

    # case_analysis_run orphan rows that were not captured by run_id lookup.
    for case_id_int in case_ids_int:
        rc = db.execute(
            text(
                """
                delete from dwp_aura.case_analysis_run
                where tenant_id = :tenant_id
                  and case_id = :case_id
                """
            ),
            {"tenant_id": tenant_id, "case_id": case_id_int},
        ).rowcount or 0
        deleted["case_analysis_run_deleted"] += int(rc)

    for h in headers:
        rc = db.execute(
            delete(AgentCase).where(
                AgentCase.tenant_id == h.tenant_id,
                AgentCase.bukrs == h.bukrs,
                AgentCase.belnr == h.belnr,
                AgentCase.gjahr == h.gjahr,
            )
        ).rowcount or 0
        deleted["agent_case_deleted"] += int(rc)

        rc = db.execute(
            delete(FiDocItem).where(
                FiDocItem.tenant_id == h.tenant_id,
                FiDocItem.bukrs == h.bukrs,
                FiDocItem.belnr == h.belnr,
                FiDocItem.gjahr == h.gjahr,
            )
        ).rowcount or 0
        deleted["fi_doc_item_deleted"] += int(rc)

        rc = db.execute(
            delete(FiDocHeader).where(
                FiDocHeader.tenant_id == h.tenant_id,
                FiDocHeader.bukrs == h.bukrs,
                FiDocHeader.belnr == h.belnr,
                FiDocHeader.gjahr == h.gjahr,
            )
        ).rowcount or 0
        deleted["fi_doc_header_deleted"] += int(rc)

    db.commit()
    # DB 삭제와 함께 메모리 런타임 상태도 정리해 동일 case_id 재사용 시 과거 run 잔상이 보이지 않게 한다.
    runtime.purge_cases(case_ids_str)
    return {
        "deleted": deleted["fi_doc_header_deleted"],
        "run_count": len(run_ids),
        **deleted,
    }
