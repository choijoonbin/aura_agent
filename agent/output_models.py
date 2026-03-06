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
    reasoning: str = Field(default="", description="실제 판단 과정 서술(스트리밍용)")


# ----- Critic -----


class CriticOutput(BaseModel):
    """비판 노드 출력. 과잉 주장·모순·누락·보류 권고·검증 대상 주장."""

    overclaim_risk: bool = Field(description="과잉 주장 위험 여부")
    contradictions: list[str] = Field(default_factory=list, description="발견된 모순 목록")
    missing_counter_evidence: list[str] = Field(default_factory=list, description="부족한 반증/누락 필드")
    recommend_hold: bool = Field(description="담당자 검토 보류 권고 여부")
    rationale: str = Field(default="", description="비판 근거")
    has_legacy_result: bool = Field(default=False, description="legacy 전문가 결과 존재 여부")
    verification_targets: list[str] = Field(
        default_factory=list,
        description="검증할 주장 문장 1~3개. evidence로 뒷받침 가능한 핵심 주장만.",
    )
    replan_required: bool = Field(
        default=False,
        description="재계획(planner 재실행) 필요 여부",
    )
    replan_reason: str = Field(
        default="",
        description="재계획이 필요한 이유",
    )
    reasoning: str = Field(default="", description="비판 근거 추론 과정 서술(스트리밍용)")


# ----- Verifier -----


class VerifierGate(str, Enum):
    READY = "READY"
    HITL_REQUIRED = "HITL_REQUIRED"
    REJECTED = "REJECTED"


class ClaimVerificationResult(BaseModel):
    """개별 검증 타겟 주장에 대한 검증 결과."""

    claim: str = Field(description="검증 대상 주장 문장 전체")
    covered: bool = Field(description="retrieval 청크로 뒷받침 가능 여부")
    supporting_articles: list[str] = Field(
        default_factory=list,
        description="이 주장을 실제로 뒷받침한 규정 조항 번호 목록",
    )
    gap: str = Field(
        default="",
        description="covered=False일 때 어떤 근거가 부족한지 설명",
    )


class VerifierOutput(BaseModel):
    """검증 노드 출력. 근거 충족·HITL 필요·게이트."""

    grounded: bool = Field(description="증거 기반으로 근거가 충족되었는지")
    needs_hitl: bool = Field(description="담당자 검토 필요 여부")
    missing_evidence: list[str] = Field(default_factory=list, description="부족한 증거 목록")
    gate: VerifierGate = Field(description="진행 게이트: READY | HITL_REQUIRED | REJECTED")
    rationale: str = Field(default="", description="검증 근거")
    quality_signals: list[str] = Field(default_factory=list, description="품질 지표 코드 목록(호환용)")
    claim_results: list[ClaimVerificationResult] = Field(
        default_factory=list,
        description="주장(claim)별 개별 검증 결과 목록 (신규)",
    )
    reasoning: str = Field(default="", description="검증 판단 과정 서술(스트리밍용)")


# ----- Execute -----


class ExecuteOutput(BaseModel):
    """실행 노드 출력. 도구 실행 요약 + 추론."""

    executed_tools: list[str] = Field(default_factory=list, description="실행된 도구 이름 목록")
    skipped_tools: list[str] = Field(default_factory=list, description="생략된 도구 이름 목록")
    failed_tools: list[str] = Field(default_factory=list, description="실패한 도구 이름 목록")
    policy_score: int = Field(default=0, description="정책 점수")
    evidence_score: int = Field(default=0, description="근거 점수")
    final_score: int = Field(default=0, description="최종 점수")
    reasoning: str = Field(default="", description="실행 결과를 다음 노드에 넘기는 추론 서술")


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
    reasoning: str = Field(default="", description="최종 판단에 이른 추론 과정(스트리밍용)")

    class Config:
        extra = "allow"


# ----- Score Breakdown -----


class ScoreSignalDetail(BaseModel):
    """개별 점수 신호 항목."""

    signal: str = Field(description="신호 식별자")
    label: str = Field(description="사용자 표시용 레이블")
    raw_value: Any = Field(default=None, description="원본 값")
    points: float = Field(description="이 신호로 부여된 점수")
    category: str = Field(description="'policy' | 'evidence' | 'multiplier' | 'amount'")


class ScoreBreakdown(BaseModel):
    """점수 산출 전체 분해 결과."""

    policy_score: float = Field(description="정책 위반 점수 (0~100)")
    evidence_score: float = Field(description="증거 품질 점수 (0~100)")
    amount_weight: float = Field(default=1.0, description="금액 구간 가중 승수 (1.0~1.3)")
    compound_multiplier: float = Field(default=1.0, description="복합 위험 승수 (1.0~1.5)")
    policy_weight: float = Field(default=0.6, description="policy_score 가중치")
    evidence_weight: float = Field(default=0.4, description="evidence_score 가중치")
    final_score: float = Field(description="최종 점수 (0~100)")
    severity: str = Field(description="'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'")
    signals: list[ScoreSignalDetail] = Field(default_factory=list, description="점수 구성 신호")
    reasons: list[str] = Field(default_factory=list, description="호환용 이유 목록")
    calculation_trace: str = Field(default="", description="최종 점수 계산식")
