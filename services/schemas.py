from __future__ import annotations

from pydantic import BaseModel, Field


class VoucherRow(BaseModel):
    voucher_key: str
    bukrs: str
    belnr: str
    gjahr: str
    amount: float | None = None
    currency: str | None = None
    demo_name: str | None = None
    merchant_name: str | None = None
    occurred_at: str | None = None
    hr_status: str | None = None
    mcc_code: str | None = None
    budget_exceeded: bool | None = None
    case_id: int | None = None
    case_type: str | None = None
    severity: str | None = None
    case_status: str | None = None


class AnalysisStartRequest(BaseModel):
    """분석 시작 시 옵션. enable_hitl=False면 HITL 팝업 없이 AI가 끝까지 분석."""

    enable_hitl: bool = True


class AnalysisStartResponse(BaseModel):
    accepted: bool = True
    run_id: str
    case_id: str
    stream_path: str


class HitlSubmitRequest(BaseModel):
    reviewer: str = Field(default="FINANCE_REVIEWER")
    comment: str | None = None
    business_purpose: str | None = None
    attendees: list[str] = Field(default_factory=list)
    approved: bool | None = None
    extra_facts: dict[str, str] = Field(default_factory=dict)


class HitlDraftRequest(BaseModel):
    reviewer: str | None = None
    comment: str | None = None
    business_purpose: str | None = None
    attendees: list[str] = Field(default_factory=list)
    approved: bool | None = None
    extra_facts: dict[str, str] = Field(default_factory=dict)


class HitlSubmitResponse(BaseModel):
    accepted: bool = True
    source_run_id: str
    resumed_run_id: str
    stream_path: str


class ReviewSubmitRequest(BaseModel):
    """HITL 팝업 통합 제출: 담당자 검토 응답 + 증빙 업로드 여부. 에이전트가 필수 항목·조건을 판단 후 분석 이어가기."""

    hitl_response: HitlSubmitRequest | None = None
    evidence_uploaded: bool = False
