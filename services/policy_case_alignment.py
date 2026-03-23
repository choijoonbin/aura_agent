from __future__ import annotations

from typing import Any

_HOLIDAY_CORE_ARTICLES = {"제23조", "제38조", "제39조"}
_LIMIT_EXCEED_CORE_ARTICLES = {"제40조", "제19조", "제11조"}

_HOLIDAY_TERMS = (
    "휴일",
    "주말",
    "공휴일",
    "심야",
    "23:00",
    "06:00",
    "시간대",
    "당직",
)

_ENTERTAINMENT_TERMS = (
    "접대비",
    "업무추진비",
    "접대 목적",
    "외부 참석자 소속",
    "외부 이해관계자",
    "외부미팅",
    "참석자 명단",
)

_TRAVEL_TERMS = (
    "출장",
    "출장비",
    "출장명령",
    "출장계획",
    "출장번호",
    "출장기간",
    "출장지",
    "교통비",
    "숙박비",
    "일비",
    "교통/숙박",
    "차량유지비",
    "유류비",
    "차량비",
    "차량 유지",
)

_LIMIT_EXCEED_TERMS = (
    "예산",
    "한도",
    "초과",
    "누적",
    "금액",
    "승인권한",
    "상위 승인",
    "차단",
)

_APPROVAL_EXCEPTION_TERMS = (
    "사후승인",
    "긴급 장애",
    "비상 상황",
    "재난 대응",
    "입력 지연",
    "지연 사유",
    "대체 증빙",
)


def _normalize_case_type(body_evidence: dict[str, Any] | None) -> str:
    body = body_evidence or {}
    return str(body.get("case_type") or body.get("intended_risk_type") or "").strip().upper()


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(token.lower() in lowered for token in tokens)


def _to_article_token(article: Any) -> str:
    return "".join(str(article or "").split())


def _ref_merged_text(ref: dict[str, Any] | None) -> str:
    item = ref or {}
    return " ".join(
        str(v or "")
        for v in (
            item.get("article") or item.get("regulation_article"),
            item.get("parent_title"),
            item.get("chunk_text"),
            item.get("clause") or item.get("regulation_clause"),
        )
    )


def is_common_evidence_article(ref: dict[str, Any] | None) -> bool:
    item = ref or {}
    article = _to_article_token(item.get("article") or item.get("regulation_article"))
    parent_title = _to_article_token(item.get("parent_title"))
    return "제14조" in article or "제14조" in parent_title


def has_entertainment_context(body_evidence: dict[str, Any] | None) -> bool:
    body = body_evidence or {}
    doc = body.get("document") or {}
    header = doc.get("header") or {}
    lines: list[str] = []
    for item in (doc.get("items") or [])[:8]:
        if isinstance(item, dict):
            lines.extend([str(item.get("sgtxt") or ""), str(item.get("hkont") or "")])
    combined = " ".join(
        str(v or "")
        for v in (
            body.get("expenseType"),
            body.get("expenseTypeName"),
            body.get("merchantName"),
            body.get("mccName"),
            body.get("businessPurpose"),
            body.get("user_reason"),
            body.get("bktxt"),
            header.get("bktxt"),
            " ".join(lines),
        )
    )
    return _contains_any(combined, _ENTERTAINMENT_TERMS)


def has_business_trip_context(body_evidence: dict[str, Any] | None) -> bool:
    body = body_evidence or {}
    doc = body.get("document") or {}
    header = doc.get("header") or {}
    lines: list[str] = []
    for item in (doc.get("items") or [])[:8]:
        if isinstance(item, dict):
            lines.extend([str(item.get("sgtxt") or ""), str(item.get("hkont") or "")])
    hr_status = str(body.get("hrStatus") or "").upper()
    if hr_status == "BUSINESS_TRIP":
        return True
    combined = " ".join(
        str(v or "")
        for v in (
            body.get("expenseType"),
            body.get("expenseTypeName"),
            body.get("merchantName"),
            body.get("mccName"),
            body.get("businessPurpose"),
            body.get("user_reason"),
            body.get("bktxt"),
            header.get("bktxt"),
            " ".join(lines),
        )
    )
    return _contains_any(combined, _TRAVEL_TERMS)


def is_clear_case_mismatch(ref: dict[str, Any], body_evidence: dict[str, Any] | None) -> bool:
    case_type = _normalize_case_type(body_evidence)
    if is_common_evidence_article(ref):
        return False

    merged = _ref_merged_text(ref)
    article = _to_article_token(ref.get("article") or ref.get("regulation_article"))

    if case_type == "HOLIDAY_USAGE":
        if has_entertainment_context(body_evidence):
            return False
        # 휴일 의심 케이스에서 실제 출장 맥락이 없으면 출장 조항/문구는 오채택으로 간주.
        # (제25조 출장비 조항이 질문 생성으로 전파되는 현상 방지)
        if not has_business_trip_context(body_evidence):
            has_travel = _contains_any(merged, _TRAVEL_TERMS) or article == "제25조"
            if has_travel:
                return True
        has_holiday = _contains_any(merged, _HOLIDAY_TERMS)
        has_entertainment = _contains_any(merged, _ENTERTAINMENT_TERMS)
        if article in _HOLIDAY_CORE_ARTICLES:
            return False
        return bool(has_entertainment and not has_holiday)

    if case_type == "LIMIT_EXCEED":
        body = body_evidence or {}
        is_holiday = bool(body.get("isHoliday"))
        occurred_at = str(body.get("occurredAt") or "")
        hour: int | None = None
        try:
            if len(occurred_at) >= 13:
                hour = int(occurred_at[11:13])
        except Exception:
            hour = None
        is_night = hour is not None and (hour >= 22 or hour < 6)

        has_limit = _contains_any(merged, _LIMIT_EXCEED_TERMS)
        has_holiday = _contains_any(merged, _HOLIDAY_TERMS) or article == "제39조"
        has_approval_exception = _contains_any(merged, _APPROVAL_EXCEPTION_TERMS) or article == "제12조"

        if article in _LIMIT_EXCEED_CORE_ARTICLES:
            return False
        # 평일·주간의 한도초과 케이스에서는 휴일/사후승인 예외 조항을 오채택으로 본다.
        if (not is_holiday and not is_night) and (has_holiday or has_approval_exception) and not has_limit:
            return True
        # 한도초과 문맥이 없는 조항인데 휴일·사후승인 예외 문맥만 강하면 오채택으로 본다.
        if (has_holiday or has_approval_exception) and not has_limit:
            return True

    return False


def case_alignment_score(ref: dict[str, Any], body_evidence: dict[str, Any] | None) -> int:
    case_type = _normalize_case_type(body_evidence)
    if not case_type:
        return 0

    article = _to_article_token(ref.get("article") or ref.get("regulation_article"))
    merged = _ref_merged_text(ref)

    score = 0
    if is_common_evidence_article(ref):
        score += 6

    if case_type == "HOLIDAY_USAGE":
        if article in _HOLIDAY_CORE_ARTICLES:
            score += 18
        if _contains_any(merged, _HOLIDAY_TERMS):
            score += 10
        if is_clear_case_mismatch(ref, body_evidence):
            score -= 40
    elif case_type == "LIMIT_EXCEED":
        if article in _LIMIT_EXCEED_CORE_ARTICLES:
            score += 18
        if _contains_any(merged, _LIMIT_EXCEED_TERMS):
            score += 10
        if is_clear_case_mismatch(ref, body_evidence):
            score -= 40

    return score
