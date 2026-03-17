from __future__ import annotations

from typing import Any

_HOLIDAY_CORE_ARTICLES = {"제23조", "제38조", "제39조"}

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


def is_clear_case_mismatch(ref: dict[str, Any], body_evidence: dict[str, Any] | None) -> bool:
    case_type = _normalize_case_type(body_evidence)
    if case_type != "HOLIDAY_USAGE":
        return False
    if is_common_evidence_article(ref):
        return False
    if has_entertainment_context(body_evidence):
        return False

    merged = _ref_merged_text(ref)
    has_holiday = _contains_any(merged, _HOLIDAY_TERMS)
    has_entertainment = _contains_any(merged, _ENTERTAINMENT_TERMS)
    article = _to_article_token(ref.get("article") or ref.get("regulation_article"))
    if article in _HOLIDAY_CORE_ARTICLES:
        return False
    return bool(has_entertainment and not has_holiday)


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

    return score

