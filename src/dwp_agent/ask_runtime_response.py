from __future__ import annotations

from datetime import datetime, timezone

from .contracts import (
    AgentRegistryResolution,
    AnswerConfidence,
    AskCitation,
    AskModelRoute,
    AskPersonalization,
    AskPolicyDecision,
    AskRequest,
    AskResponse,
    AskState,
)
from .policy import AskIdentity


def build_ask_response(
    *,
    request: AskRequest,
    identity: AskIdentity,
    run_id: str,
    audit_id: str,
    registry: AgentRegistryResolution,
    policy: AskPolicyDecision,
    state: AskState,
    status_code: str,
    model_route: AskModelRoute,
    answer: str | None = None,
    confidence: AnswerConfidence | None = None,
    citations: list[AskCitation] | None = None,
    source_count: int = 0,
    personalization: AskPersonalization | None = None,
) -> AskResponse:
    return AskResponse(
        run_id=run_id,
        audit_id=audit_id,
        request_id=request.request_id,
        correlation_id=identity.correlation_id,
        state=state,
        answer=answer,
        confidence=confidence,
        citations=citations or [],
        source_count=source_count,
        policy=policy,
        model_route=model_route,
        agent_registry=registry,
        status_code=status_code,
        completed_at=datetime.now(timezone.utc),
        selected_work=request.page_context.selected_work if request.page_context else None,
        personalization=personalization or AskPersonalization(),
    )
