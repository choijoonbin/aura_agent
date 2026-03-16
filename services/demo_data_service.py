from __future__ import annotations

import hashlib
import json
import logging
import random
import uuid as _uuid_module
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, select, text
from sqlalchemy.orm import Session

from db.models import AgentCase, FiDocHeader, FiDocItem
from services.stream_runtime import runtime
from utils.config import settings

logger = logging.getLogger(__name__)

# Beta 전용 저장 경로 (POC 로컬 파일시스템)
_EVIDENCE_UPLOAD_ROOT = Path("data/evidence_uploads")


def _combine_date_time(date_str: str, time_str: str) -> str:
    """날짜(YYYY-MM-DD)와 시간(HH:MM)을 합쳐 ISO 8601 datetime 문자열 반환.

    - date_str이 없으면 빈 문자열 반환.
    - time_str이 없으면 날짜만 반환 (YYYY-MM-DD).
    - 유효하지 않은 형식은 무시하고 파싱 가능한 범위만 조합.
    예: ("2026-03-14", "19:45") → "2026-03-14T19:45"
    예: ("2026-03-14", "")     → "2026-03-14"
    """
    date_clean = date_str.strip()
    time_clean = time_str.strip()
    if not date_clean:
        return ""
    try:
        datetime.strptime(date_clean, "%Y-%m-%d")
    except ValueError:
        return date_clean
    if not time_clean:
        return date_clean
    # HH:MM 또는 HH:MM:SS 허용
    try:
        t = datetime.strptime(time_clean, "%H:%M")
        return f"{date_clean}T{t.strftime('%H:%M')}"
    except ValueError:
        pass
    try:
        t = datetime.strptime(time_clean, "%H:%M:%S")
        return f"{date_clean}T{t.strftime('%H:%M')}"
    except ValueError:
        pass
    # 파싱 실패 시 날짜만 반환
    return date_clean


# 시연 데이터 규칙:
# - AuraAgent 표준 case_type 5종(HOLIDAY_USAGE/LIMIT_EXCEED/PRIVATE_USE_RISK/UNUSUAL_PATTERN/NORMAL_BASELINE)만 사용.
# - 스크리닝 핵심 입력 필드(occurredAt, isHoliday, hrStatus, hrStatusRaw, mccCode, budgetExceeded, amount)를
#   생성 단계에서 모두 채우도록 시나리오 프로파일을 강제 검증한다.
# - 정상 비교군(NORMAL_BASELINE)은 분석 시 HITL 없이 완료되도록 규정에 걸리지 않는 값으로 고정:
#   평일(day_mode=weekday), 낮 시간(hour_candidates=11~13), hr_status=WORK, mcc_code=5816(위험 MCC 아님), budget=N.
SCENARIO_PROFILES: dict[str, dict[str, Any]] = {
    "HOLIDAY_USAGE": {
        "label": "휴일 사용 의심",
        "description": "주말/휴무일 식당 사용 시나리오",
        "blart": "SA",
        "hr_status": "LEAVE",  # BE: weekend→OFF, calendar or fallback
        "mcc_code": "5813",   # BE preferred: 5813, 5812, 5814
        "budget_flag": "N",   # BE: HOLIDAY_USAGE는 N
        "merchant_name": "가온 식당",
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

# OCR 검증/시연 일관성을 위한 고정 생성값(비정상 시나리오 전용)
_FIXED_WEEKEND_DATE = date(2026, 3, 14)  # 2026-03-14 (토)
_FIXED_OCCUR_TIME = time(19, 45, 0)
_FIXED_AMOUNT_KRW = 97042

# Beta 경로 belnr 접두사 (Legacy와 구별: H/L/P/U/N → BH/BL/BP/BU/BN)
_BETA_BELNR_PREFIX: dict[str, str] = {
    "HOLIDAY_USAGE": "BH",
    "LIMIT_EXCEED": "BL",
    "PRIVATE_USE_RISK": "BP",
    "UNUSUAL_PATTERN": "BU",
    "NORMAL_BASELINE": "BN",
}


# ──────────────────────────────────────────────────────────────────────────────
# Beta: 시연 데이터 생성 서비스 (기존 seed 로직과 분리된 독립 경로)
# ──────────────────────────────────────────────────────────────────────────────

# 케이스 유형별 표준 검토 질문 (에이전트 규정 기반과 동일 구조)
_REVIEW_QUESTIONS_BY_CASE_TYPE: dict[str, dict[str, Any]] = {
    "HOLIDAY_USAGE": {
        "required_inputs": [
            {"field": "approval_doc", "reason": "휴일 사용 사전 승인 규정(제4조)", "guide": "휴일 사전 승인 문서를 첨부하세요"},
            {"field": "business_purpose", "reason": "업무 목적 입증 필요(제6조)", "guide": "업무 목적을 구체적으로 기술하세요"},
        ],
        "review_questions": [
            "휴일 사용에 대한 사전 승인을 받았습니까?",
            "해당 지출이 업무 목적임을 증명할 수 있습니까?",
        ],
    },
    "LIMIT_EXCEED": {
        "required_inputs": [
            {"field": "approval_over_limit", "reason": "한도 초과 결재 승인 필요(제9조)", "guide": "한도 초과 승인 결재 문서를 첨부하세요"},
            {"field": "counterpart_info", "reason": "접대 상대방 정보 필수(제10조)", "guide": "접대 상대방 및 목적을 기재하세요"},
        ],
        "review_questions": [
            "한도 초과에 대한 결재 승인이 있습니까?",
            "접대 상대방 및 목적을 명시할 수 있습니까?",
        ],
    },
    "PRIVATE_USE_RISK": {
        "required_inputs": [
            {"field": "work_relation_proof", "reason": "업무 관련성 증명 필요(제7조)", "guide": "업무 관련 근거 자료를 제출하세요"},
            {"field": "non_private_declaration", "reason": "사적 사용 아님 확인 필요(제8조)", "guide": "사적 사용이 아님을 확인하는 서술을 작성하세요"},
        ],
        "review_questions": [
            "해당 지출이 업무와 직접 관련된 것임을 증명할 수 있습니까?",
            "사적 사용이 아닌 업무 목적 사용 근거가 있습니까?",
        ],
    },
    "UNUSUAL_PATTERN": {
        "required_inputs": [
            {"field": "reason_for_unusual_time", "reason": "심야/비정상 시간대 불가피한 사유 필요(제5조)", "guide": "심야 사용이 불가피했던 업무 사유를 기술하세요"},
            {"field": "industry_purpose", "reason": "해당 업종 이용 업무 목적 확인(제6조)", "guide": "해당 업종 이용이 업무 목적임을 설명하세요"},
        ],
        "review_questions": [
            "심야/비정상 시간대 사용에 대한 업무상 불가피한 사유가 있습니까?",
            "해당 업종 이용이 업무 목적임을 설명할 수 있습니까?",
        ],
    },
    "NORMAL_BASELINE": {
        "required_inputs": [],
        "review_questions": [],
    },
}


def is_generate_disabled(all_valid: bool, is_abnormal: bool, has_file: bool) -> bool:
    """버튼 비활성화 조건: 5개 필드 미완료 OR 비정상 케이스+파일 미첨부."""
    if is_abnormal and not has_file:
        return True
    return not all_valid


def validate_demo_required_fields(
    amount: str,
    date_occ: str,
    merchant: str,
    bktxt: str,
    user_reason: str,
) -> tuple[bool, list[str]]:
    """
    시연 데이터 생성 필수 5개 항목 유효성 검사. (UI 레이어에서 재사용 가능한 순수 함수)

    Returns:
        (all_valid: bool, errors: list[str])
    """
    errors: list[str] = []

    try:
        if not (amount.replace(",", "").strip() and float(amount.replace(",", "")) > 0):
            errors.append("금액: 0 초과 숫자를 입력하세요")
    except (ValueError, AttributeError):
        errors.append("금액: 0 초과 숫자를 입력하세요")

    try:
        datetime.strptime(date_occ.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        errors.append("일자: YYYY-MM-DD 형식으로 입력하세요")

    if not merchant.strip():
        errors.append("가맹점명을 입력하세요")
    if not bktxt.strip():
        errors.append("적요를 입력하세요")
    if not user_reason.strip():
        errors.append("사유를 입력하세요")

    return len(errors) == 0, errors


def generate_preview_questions(case_type: str, case_data: dict[str, Any]) -> dict[str, Any]:
    """
    케이스 유형에 맞는 규정 기반 검토 질문과 필수 입력 목록을 반환한다.
    agent/langgraph_verification_logic.py의 규정 기반 질문 구조와 동일한 형태.

    Returns:
        {
            "required_inputs": [{"field": ..., "reason": ..., "guide": ...}, ...],
            "review_questions": ["질문1", "질문2", ...],
        }
    """
    base = _REVIEW_QUESTIONS_BY_CASE_TYPE.get(
        case_type,
        {"required_inputs": [], "review_questions": []},
    )
    return {
        "required_inputs": list(base["required_inputs"]),
        "review_questions": list(base["review_questions"]),
    }


def _create_beta_voucher(
    db: Session,
    payload: dict[str, Any],
    case_type: str,
    case_uuid: str,
) -> str:
    """
    Beta 경로 전용: FiDocHeader/FiDocItem DB 전표를 생성하고 스크리닝을 수행한다.

    - 정책 필드(hr_status, mcc_code, budget_flag, blart, waers)는 SCENARIO_PROFILES 기본값 사용.
    - 이미지 추출 필드(amount, merchant, occurredAt)는 payload 값 우선, 실패 시 시나리오 기본값 사용.

    Returns:
        voucher_key (예: "1000-BH000000001-2026")
    """
    from services.case_service import run_case_screening

    tenant_id = settings.default_tenant_id
    user_id = settings.default_user_id
    profile = SCENARIO_PROFILES.get(case_type)
    if not profile:
        raise ValueError(f"_create_beta_voucher: unknown case_type={case_type!r}")

    # 날짜 파싱 (payload 우선, 실패 시 시나리오 기본값)
    try:
        budat = date.fromisoformat(payload.get("date_occurrence", "").strip())
    except (ValueError, TypeError, AttributeError):
        budat = _next_day(profile["day_mode"])
    gjahr_str = str(budat.year)

    # 시간 파싱 (payload 우선, 실패 시 시나리오 기본값)
    time_str = (payload.get("time_occurrence") or "").strip()
    cputm: time | None = None
    if time_str:
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                cputm = datetime.strptime(time_str, fmt).time()
                break
            except ValueError:
                continue
    if cputm is None:
        cputm = time(
            hour=random.choice(profile["hour_candidates"]),
            minute=random.randint(0, 59),
            second=0,
        )

    # 금액 파싱 (payload 우선, 실패 시 시나리오 기본값)
    amount_str = (payload.get("amount_total") or "").replace(",", "").strip()
    try:
        amount_krw = float(amount_str)
        if amount_krw <= 0:
            raise ValueError("non-positive amount")
    except (ValueError, TypeError):
        amount_krw = float(random.randint(*profile["amount_range"]))

    # belnr 생성: 접두사 + 8자리 순번 (= 10자리)
    beta_prefix = _BETA_BELNR_PREFIX.get(case_type, "BX")
    existing_count = db.scalar(
        select(func.count(FiDocHeader.belnr)).where(
            FiDocHeader.tenant_id == tenant_id,
            FiDocHeader.bukrs == "1000",
            FiDocHeader.belnr.like(f"{beta_prefix}%"),
            FiDocHeader.gjahr == gjahr_str,
        )
    ) or 0
    seq = existing_count + 1
    belnr = f"{beta_prefix}{seq:08d}"

    # bktxt / sgtxt
    bktxt = (payload.get("bktxt") or "").strip()
    if not bktxt:
        bktxt = f"{profile['label']}{_eul_reul(profile['label'])} 위한 시연 데이터 ({belnr})"
    sgtxt = (payload.get("sgtxt") or "").strip() or profile.get("item_text", "")
    xblnr = f"BETA-{case_type[:8]}-{belnr}"[:30]

    now = datetime.now(timezone.utc)
    header = FiDocHeader(
        tenant_id=tenant_id,
        bukrs="1000",
        belnr=belnr,
        gjahr=gjahr_str,
        user_id=user_id,
        doc_source="BETA",
        budat=budat,
        cpudt=budat,
        cputm=cputm,
        blart=profile["blart"],
        waers="KRW",
        bktxt=bktxt,
        xblnr=xblnr,
        intended_risk_type=None,
        hr_status=profile["hr_status"],
        mcc_code=profile["mcc_code"],
        budget_exceeded_flag=profile["budget_flag"],
        created_at=now,
        updated_at=now,
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
        wrbtr=amount_krw,
        waers="KRW",
        lifnr="C1001",
        sgtxt=sgtxt,
    )
    db.add(header)
    db.add(item)

    voucher_key = f"1000-{belnr}-{gjahr_str}"
    try:
        db.flush()
        run_case_screening(db, voucher_key, strict_required_fields=True, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info(
        "save_custom_demo_case: DB voucher created (voucher_key=%s, uuid=%s)",
        voucher_key,
        case_uuid,
    )
    return voucher_key


def save_custom_demo_case(
    payload: dict[str, Any],
    image_bytes: bytes,
    filename: str,
    db: Session | None = None,
) -> dict[str, Any]:
    """
    업로드 증빙 기반 커스텀 시연 케이스를 data/evidence_uploads/{uuid}/ 에 저장한다.
    기존 seed_demo_scenarios()와 완전히 분리된 Beta 전용 저장 경로.

    Args:
        payload: 케이스 메타데이터 (case_type, amount_total, date_occurrence, merchant_name, bktxt, sgtxt, user_reason 등)
        image_bytes: 이미지 파일 내용 (없으면 b"")
        filename: 원본 파일명
        db: SQLAlchemy Session. 제공 시 FiDocHeader/FiDocItem 전표 생성 및 스크리닝 수행.

    Returns:
        {"case_uuid": str, "image_path": str, "meta_path": str, "created_at": str}
        db 제공 시 "voucher_key" 추가 포함.
    """
    case_uuid = str(_uuid_module.uuid4())
    save_dir = _EVIDENCE_UPLOAD_ROOT / case_uuid
    save_dir.mkdir(parents=True, exist_ok=True)

    # 이미지 저장
    image_path = ""
    if image_bytes:
        ext = Path(filename).suffix.lower() if filename else ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = ".jpg"
        img_filename = f"evidence{ext}"
        img_path = save_dir / img_filename
        img_path.write_bytes(image_bytes)
        image_path = str(img_path)
        logger.info("save_custom_demo_case: image saved to %s", image_path)

    # 메타데이터 JSON 저장
    now_iso = datetime.now(timezone.utc).isoformat()
    case_type = payload.get("case_type", "UNKNOWN")
    q_data = generate_preview_questions(case_type, payload)

    meta: dict[str, Any] = {
        "case_uuid": case_uuid,
        "created_at": now_iso,
        "model": payload.get("model_source", "vision_llm"),
        "fallback_used": payload.get("fallback_used", False),
        "extracted_entities": payload.get("extracted_entities", []),
        "edited_entities": {
            "amount_total": payload.get("amount_total", ""),
            "date_occurrence": payload.get("date_occurrence", ""),
            "time_occurrence": payload.get("time_occurrence", ""),
            "datetime_occurrence": _combine_date_time(
                payload.get("date_occurrence", ""),
                payload.get("time_occurrence", ""),
            ),
            "merchant_name": payload.get("merchant_name", ""),
            "mcc_code": payload.get("mcc_code", ""),
        },
        "mcc_code": payload.get("mcc_code", ""),
        "case_type": case_type,
        "review_questions": payload.get("review_questions") or q_data["review_questions"],
        "review_answers": payload.get("review_answers", []),
        "image_path": image_path,
        "memo": {
            "bktxt": payload.get("bktxt", ""),
            "sgtxt": payload.get("sgtxt", ""),
            "user_reason": payload.get("user_reason", ""),
        },
    }

    # DB 전표 생성 (db 세션이 제공된 경우)
    voucher_key: str | None = None
    if db is not None:
        voucher_key = _create_beta_voucher(db, payload, case_type, case_uuid)
        meta["voucher_key"] = voucher_key

    meta_path = save_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("save_custom_demo_case: meta saved to %s (uuid=%s)", meta_path, case_uuid)

    result: dict[str, Any] = {
        "case_uuid": case_uuid,
        "image_path": image_path,
        "meta_path": str(meta_path),
        "created_at": now_iso,
    }
    if voucher_key:
        result["voucher_key"] = voucher_key
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Legacy: 기존 시나리오 seed 로직 (재사용 가능하도록 분리 유지)
# ──────────────────────────────────────────────────────────────────────────────

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
    if mode == "weekend":
        return _FIXED_WEEKEND_DATE

    # 기존 주말 동적 계산 로직(원복 참고용):
    # target = date.today()
    # if mode == "weekend":
    #     while target.weekday() != 5:
    #         target = date.fromordinal(target.toordinal() + 1)
    #     return target

    target = date.today()
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
        is_normal_baseline = scenario == "NORMAL_BASELINE"
        occur_time = (
            time(
                hour=random.choice(profile["hour_candidates"]),
                minute=random.randint(0, 59),
                second=random.randint(0, 59),
            )
            if is_normal_baseline
            else _FIXED_OCCUR_TIME
        )
        amount_krw = random.randint(*profile["amount_range"]) if is_normal_baseline else _FIXED_AMOUNT_KRW

        header = FiDocHeader(
            tenant_id=tenant_id,
            bukrs="1000",
            belnr=belnr,
            gjahr=gjahr_str,
            user_id=user_id,
            doc_source="POC",
            budat=target_day,
            cpudt=target_day,
            cputm=occur_time,
            blart=profile["blart"],
            waers="KRW",
            bktxt=f"{profile['label']}{_eul_reul(profile['label'])} 위한 테스트 데이터 ({belnr})",
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
            wrbtr=amount_krw,
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
        raw_scenario = (header.xblnr or "").replace("DEMO-", "") if header.xblnr else "-"
        scenario = raw_scenario.rsplit("-", 1)[0] if "-" in raw_scenario else raw_scenario
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
