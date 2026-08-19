from datetime import datetime, timezone
from uuid import UUID

import pytest

from dwp_agent.contracts import (
    AgentRegistryResolution,
    AnswerConfidence,
    AnswerFeedbackRequest,
    AskCitation,
    AskModelRoute,
    AskPolicyDecision,
    AskResponse,
    CitationSourceType,
    ModelRouteState,
    PolicyOutcome,
    RegistryResolutionStatus,
    RegistryRiskTier,
    RiskTier,
)
from dwp_agent.conversation_store import ConversationNotFound, InMemoryConversationStore
from dwp_agent.workplace_actions import available_workplace_actions, resolve_workplace_action


def response() -> AskResponse:
    return AskResponse(
        run_id="d6df3a1c-326d-45d1-a3ac-4dcdd8ed694a",
        audit_id="audit-1",
        request_id="request-1",
        correlation_id="correlation-1",
        state="COMPLETED",
        answer="Your next meeting starts at 15:00.",
        confidence=AnswerConfidence.HIGH,
        citations=[
            AskCitation(
                source_id="src-01",
                source_type=CitationSourceType.CALENDAR,
                title="Project review",
                source_system="DWP Calendar",
                excerpt="startsAt=2026-08-19T15:00:00+09:00",
            )
        ],
        source_count=1,
        policy=AskPolicyDecision(
            outcome=PolicyOutcome.ALLOW,
            risk_tier=RiskTier.L1,
            code="ASK_READ_ALLOWED",
            explanation="Read-only query is permitted.",
            model_allowed=True,
        ),
        model_route=AskModelRoute(
            state=ModelRouteState.COMPLETED,
            provider="OPENAI",
            model="gpt-test",
        ),
        agent_registry=AgentRegistryResolution(
            entry_key="DWP_ASSISTANT",
            revision=1,
            artifact_version="test",
            risk_tier=RegistryRiskTier.MEDIUM,
            resolution=RegistryResolutionStatus.ACTIVE,
        ),
        status_code="ANSWER_GROUNDED",
        completed_at=datetime.now(timezone.utc),
    )


def test_conversation_store_is_user_scoped_idempotent_and_feedback_capable() -> None:
    store = InMemoryConversationStore()
    conversation_id = store.ensure(
        tenant_id="1",
        user_id="7",
        conversation_id=None,
        locale="ko",
        initial_query="오늘 다음 일정은 무엇인가요?",
    )
    first_pair = store.append_exchange(
        tenant_id="1",
        user_id="7",
        conversation_id=conversation_id,
        request_id="request-1",
        query="오늘 다음 일정은 무엇인가요?",
        response=response(),
    )
    replay_pair = store.append_exchange(
        tenant_id="1",
        user_id="7",
        conversation_id=conversation_id,
        request_id="request-1",
        query="오늘 다음 일정은 무엇인가요?",
        response=response(),
    )

    assert first_pair == replay_pair
    detail = store.get(tenant_id="1", user_id="7", conversation_id=conversation_id)
    assert detail.summary.message_count == 2
    assert [message.role for message in detail.messages] == ["USER", "ASSISTANT"]
    assert detail.messages[1].citations[0].excerpt
    receipt = store.feedback(
        tenant_id="1",
        user_id="7",
        run_id=UUID(response().run_id),
        request=AnswerFeedbackRequest(rating="UP"),
    )
    assert receipt.rating == "UP"

    with pytest.raises(ConversationNotFound):
        store.get(tenant_id="1", user_id="8", conversation_id=conversation_id)


def test_workplace_action_registry_filters_exact_verified_permissions() -> None:
    permissions = (
        "APP.ASK:VIEW",
        "APP.CALENDAR:CREATE",
        "ACTION.APPROVAL_REQUEST:CREATE",
    )

    actions = available_workplace_actions(permissions)

    assert [action.action_key for action in actions] == [
        "CALENDAR.EVENT.CREATE",
        "APPROVAL.REQUEST.CREATE",
    ]
    assert resolve_workplace_action(
        "calendar.event.create", permissions
    ).target_route == "/calendar/schedule?create=event"
