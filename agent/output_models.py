"""
Phase B: planner / critic / verifier / reporter용 structured output 스키마.
공식 문서 8.2 스키마 예시 기반. 자유문장만 반환하지 않도록 노드가 이 스키마를 채워 반환한다.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ----- Planner -----


class PlanStep(BaseModel):
    """단일 조사 단계."""

    tool_name: str = Field(description="호출할 도구 이름")
    purpose: str = Field(description="조사 목적/이유")
    required: bool = Field(default=True, description="필수 실행 여부")
    skip_condition: str | None = Field(default=None, description="생략 조건 설명")
    owner: str | None = Field(default=None, description="담당 역할(planner/specialist 등)")


class PlannerOutput(BaseModel):
    """플래너 노드 출력. 계획 목표·단계·예산·근거."""

    objective: str = Field(description="이번 분석의 목표")
    steps: list[PlanStep] = Field(default_factory=list, description="실행할 도구 단계 목록")
    stop_after_sufficient_evidence: bool = Field(default=True, description="증거 충분 시 조기 종료 여부")
    tool_budget: int | None = Field(default=None, description="최대 도구 호출 수(없으면 무제한)")
    rationale: str = Field(default="", description="계획 수립 근거")


# ----- Critic -----


class CriticOutput(BaseModel):
    """비판 노드 출력. 과잉 주장·모순·누락·보류 권고."""

    overclaim_risk: bool = Field(description="과잉 주장 위험 여부")
    contradictions: list[str] = Field(default_factory=list, description="발견된 모순 목록")
    missing_counter_evidence: list[str] = Field(default_factory=list, description="부족한 반증/누락 필드")
    recommend_hold: bool = Field(description="사람 검토 보류 권고 여부")
    rationale: str = Field(default="", description="비판 근거")
    # 기존 critique 호환용 추가 필드
    has_legacy_result: bool = Field(default=False, description="legacy 전문가 결과 존재 여부")


# ----- Verifier -----


class VerifierGate(str, Enum):
    READY = "READY"
    HITL_REQUIRED = "HITL_REQUIRED"
    REJECTED = "REJECTED"


class VerifierOutput(BaseModel):
    """검증 노드 출력. 근거 충족·HITL 필요·게이트."""

    grounded: bool = Field(description="증거 기반으로 근거가 충족되었는지")
    needs_hitl: bool = Field(description="사람 검토 필요 여부")
    missing_evidence: list[str] = Field(default_factory=list, description="부족한 증거 목록")
    gate: VerifierGate = Field(description="진행 게이트: READY | HITL_REQUIRED | REJECTED")
    rationale: str = Field(default="", description="검증 근거")
    quality_signals: list[str] = Field(default_factory=list, description="품질 신호(호환용)")


# ----- Reporter -----


class Citation(BaseModel):
    """문장에 연결된 규정 인용."""

    chunk_id: str | None = Field(default=None, description="정책 청크 ID")
    article: str = Field(description="조항 식별자")
    title: str | None = Field(default=None, description="조항/문서 제목")


class ReporterSentence(BaseModel):
    """문장 단위 보고 + 인용."""

    sentence: str = Field(description="보고 문장")
    citations: list[Citation] = Field(default_factory=list, description="연결된 인용")


class ReporterOutput(BaseModel):
    """리포터 노드 출력. 요약·판정·문장별 인용."""

    summary: str = Field(description="사용자용 요약")
    verdict: str = Field(default="", description="판정(예: HITL_REQUIRED, READY)")
    sentences: list[ReporterSentence] = Field(default_factory=list, description="문장별 보고 + 인용")

    class Config:
        extra = "allow"
