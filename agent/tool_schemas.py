"""
Phase A: LangChain tool 입력/출력 스키마.
모든 실행 capability는 이 스키마를 갖춘 tool로 노출된다.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolContextInput(BaseModel):
    """도구 호출 시 공통 입력. LangChain tool input schema."""

    case_id: str = Field(description="분석 대상 케이스 ID")
    body_evidence: dict[str, Any] = Field(default_factory=dict, description="전표/입력 증거 (occurredAt, amount, mccCode, document 등)")
    intended_risk_type: str | None = Field(default=None, description="스크리닝된 위험 유형")
    prior_tool_results: list[dict[str, Any]] = Field(
        default_factory=list,
        description="현재 도구 호출 이전에 완료된 도구 결과 목록 (상호참조용). 각 원소는 {tool, ok, facts, summary} 형태.",
    )


# 하위 호환: 기존 import 유지
SkillContextInput = ToolContextInput


class ToolResultEnvelope(BaseModel):
    """도구 실행 결과 공통 봉투. LangChain tool result schema."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    tool: str = Field(description="실행된 도구 이름", alias="skill")
    ok: bool = Field(description="실행 성공 여부")
    facts: dict[str, Any] = Field(default_factory=dict, description="수집된 사실/증거")
    summary: str = Field(default="", description="한 줄 요약")
