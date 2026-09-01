from __future__ import annotations

import re

from .context_broker import GroundedContext
from .contracts import AnswerConfidence
from .model_gateway import ModelAnswer


MAX_FALLBACK_SOURCES = 5
MAX_FALLBACK_EXCERPT = 240
_METADATA = re.compile(r"^([A-Za-z][A-Za-z0-9]*)=(.*)$")
_PRIORITY = {"CRITICAL": 90, "URGENT": 80, "HIGH": 35, "MEDIUM": 10, "LOW": 0}


def grounded_evidence_fallback(context: GroundedContext, locale: str) -> ModelAnswer:
    korean = locale.lower().startswith("ko")
    selected = tuple(
        sorted(context.sources, key=lambda source: (-_priority_score(source.evidence), source.rank))[
            :MAX_FALLBACK_SOURCES
        ]
    )
    heading = (
        "AI 추론 연결이 일시적으로 원활하지 않아, 현재 권한으로 확인된 업무 근거를 "
        "우선순위 순서로 보여드립니다."
        if korean
        else "AI reasoning is temporarily unavailable. Here are the highest-ranked work "
        "items verified within your current access scope."
    )
    lines = [heading]
    for index, source in enumerate(selected, start=1):
        excerpt, priority = _presentable_evidence(source.citation.excerpt or "")
        label = _priority_label(priority, korean)
        detail = f" - {excerpt}" if excerpt else ""
        lines.append(
            f"{index}. {label}{source.citation.title}{detail} [{source.citation.source_id}]"
        )
    closing = (
        "연결이 복구되면 같은 질문을 다시 실행해 비교·요약된 답변을 받을 수 있습니다."
        if korean
        else "Retry after the model connection recovers for a synthesized comparison."
    )
    lines.append(closing)
    return ModelAnswer(
        answer="\n".join(lines),
        cited_source_ids=tuple(source.citation.source_id for source in selected),
        confidence=AnswerConfidence.LOW,
        abstain_reason=None,
        provider="DWP_GROUNDED_FALLBACK",
        model="evidence-snapshot-v1",
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        latency_ms=0,
        provider_request_hash=None,
    )


def _presentable_evidence(value: str) -> tuple[str, str | None]:
    narrative: list[str] = []
    metadata: dict[str, str] = {}
    for segment in value.split(" | "):
        normalized = " ".join(segment.split()).strip()
        if not normalized:
            continue
        match = _METADATA.fullmatch(normalized)
        if match:
            metadata[match.group(1).lower()] = match.group(2).strip()
        else:
            narrative.append(normalized)
    excerpt = " ".join(narrative)[:MAX_FALLBACK_EXCERPT]
    priority = metadata.get("priority") or metadata.get("importance")
    return excerpt, priority.upper() if priority else None


def _priority_score(value: str) -> int:
    _, priority = _presentable_evidence(value)
    normalized = value.upper()
    score = _PRIORITY.get(priority or "", 0)
    if "STATUS=OVERDUE" in normalized:
        score += 45
    elif "STATUS=DUE_SOON" in normalized:
        score += 30
    if "CONFLICT=TRUE" in normalized or "RESPONSE=NEEDS_ACTION" in normalized:
        score += 20
    if "UNREAD=TRUE" in normalized:
        score += 5
    return score


def _priority_label(priority: str | None, korean: bool) -> str:
    if priority in {"URGENT", "CRITICAL"}:
        return "[긴급] " if korean else "[Urgent] "
    if priority == "HIGH":
        return "[높은 우선순위] " if korean else "[High priority] "
    return ""
