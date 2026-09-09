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
from dwp_agent.conversation_store import (
    ConversationNotFound,
    ConversationRetentionLocked,
    ConversationStoreUnavailable,
    InMemoryConversationStore,
)
from dwp_agent.run_store import InMemoryRunStore, RunLease, RunStart
from dwp_agent.workplace_actions import available_workplace_actions, resolve_workplace_action


def response(
    *,
    run_id: str = "d6df3a1c-326d-45d1-a3ac-4dcdd8ed694a",
    answer: str = "Your next meeting starts at 15:00.",
) -> AskResponse:
    return AskResponse(
        run_id=run_id,
        audit_id="audit-1",
        request_id="request-1",
        correlation_id="correlation-1",
        state="COMPLETED",
        answer=answer,
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
    result = response()
    run_store = InMemoryRunStore()
    lease = begin(run_store, result)
    store = InMemoryConversationStore(run_store)
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
        response=result,
        lease=lease,
    )
    replay_pair = store.append_exchange(
        tenant_id="1",
        user_id="7",
        conversation_id=conversation_id,
        request_id="request-1",
        query="오늘 다음 일정은 무엇인가요?",
        response=result,
        lease=lease,
    )

    assert first_pair == replay_pair
    assert store.list(tenant_id="1", user_id="7") == []
    with pytest.raises(ConversationNotFound):
        store.get(tenant_id="1", user_id="7", conversation_id=conversation_id)
    result = result.model_copy(
        update={
            "conversation_id": conversation_id,
            "user_message_id": first_pair[0],
            "assistant_message_id": first_pair[1],
        }
    )
    run_store.complete(result, lease=lease, tenant_id="1", user_id="7")
    detail = store.get(tenant_id="1", user_id="7", conversation_id=conversation_id)
    assert detail.summary.message_count == 2
    assert detail.summary.agent_key == "DWP_ASSISTANT"
    assert detail.summary.source_systems == ["DWP Calendar"]
    assert detail.summary.evidence_count == 1
    assert detail.summary.summary_excerpt == result.answer
    assert detail.summary.last_answer_status == "ANSWER_GROUNDED"
    assert detail.summary.retention_until is not None
    assert detail.summary.retention_until > detail.summary.created_at
    assert detail.summary.legal_hold is False
    assert store.list(tenant_id="1", user_id="7") == [detail.summary]
    assert [message.role for message in detail.messages] == ["USER", "ASSISTANT"]
    assert detail.messages[1].citations[0].excerpt
    receipt = store.feedback(
        tenant_id="1",
        user_id="7",
        run_id=UUID(result.run_id),
        request=AnswerFeedbackRequest(rating="UP"),
    )
    assert receipt.rating == "UP"

    with pytest.raises(ConversationNotFound):
        store.get(tenant_id="1", user_id="8", conversation_id=conversation_id)


def test_in_memory_legal_hold_metadata_blocks_deletion() -> None:
    result = response()
    run_store = InMemoryRunStore()
    lease = begin(run_store, result)
    store = InMemoryConversationStore(run_store, legal_hold=True)
    conversation_id = store.ensure(
        tenant_id="1",
        user_id="7",
        conversation_id=None,
        locale="ko",
        initial_query="법적 보존 대화",
    )
    pair = store.append_exchange(
        tenant_id="1",
        user_id="7",
        conversation_id=conversation_id,
        request_id=result.request_id,
        query="법적 보존 대화",
        response=result,
        lease=lease,
    )
    run_store.complete(
        result.model_copy(
            update={
                "conversation_id": conversation_id,
                "user_message_id": pair[0],
                "assistant_message_id": pair[1],
            }
        ),
        lease=lease,
        tenant_id="1",
        user_id="7",
    )

    assert store.get(
        tenant_id="1", user_id="7", conversation_id=conversation_id
    ).summary.legal_hold is True
    with pytest.raises(ConversationRetentionLocked):
        store.delete(tenant_id="1", user_id="7", conversation_id=conversation_id)


def test_stale_in_memory_exchange_is_hidden_and_replaced_by_the_new_owner() -> None:
    run_store = InMemoryRunStore()
    stale_response = response()
    stale_lease = begin(run_store, stale_response)
    store = InMemoryConversationStore(run_store)
    conversation_id = store.ensure(
        tenant_id="1",
        user_id="7",
        conversation_id=None,
        locale="ko",
        initial_query="lease visibility",
    )
    store.append_exchange(
        tenant_id="1",
        user_id="7",
        conversation_id=conversation_id,
        request_id=stale_response.request_id,
        query="lease visibility",
        response=stale_response,
        lease=stale_lease,
    )
    run_store.fail(stale_lease, "LEASE_EXPIRED")

    with pytest.raises(ConversationStoreUnavailable, match="no longer owned"):
        store.append_exchange(
            tenant_id="1",
            user_id="7",
            conversation_id=conversation_id,
            request_id=stale_response.request_id,
            query="lease visibility",
            response=stale_response,
            lease=stale_lease,
        )
    assert store.list(tenant_id="1", user_id="7") == []
    with pytest.raises(ConversationNotFound):
        store.get(tenant_id="1", user_id="7", conversation_id=conversation_id)

    active_response = response(
        run_id=stale_response.run_id,
        answer="Only the active owner is visible.",
    )
    active_lease = begin(run_store, active_response)
    pair = store.append_exchange(
        tenant_id="1",
        user_id="7",
        conversation_id=conversation_id,
        request_id=active_response.request_id,
        query="lease visibility",
        response=active_response,
        lease=active_lease,
    )
    active_response = active_response.model_copy(
        update={
            "conversation_id": conversation_id,
            "user_message_id": pair[0],
            "assistant_message_id": pair[1],
        }
    )
    run_store.complete(
        active_response,
        lease=active_lease,
        tenant_id="1",
        user_id="7",
    )

    detail = store.get(tenant_id="1", user_id="7", conversation_id=conversation_id)
    assert detail.summary.message_count == 2
    assert [message.content for message in detail.messages] == [
        "lease visibility",
        "Only the active owner is visible.",
    ]


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


def begin(run_store: InMemoryRunStore, result: AskResponse) -> RunLease:
    lease = run_store.begin(
        RunStart(
            run_id=result.run_id,
            tenant_id="1",
            user_id="7",
            request_id=result.request_id,
            query_hash="a" * 64,
            agent_key="DWP_ASSISTANT",
            agent_revision=1,
            risk_tier="L1",
            policy_outcome="ALLOW",
            locale="ko",
            correlation_id=result.correlation_id,
        )
    )
    assert lease is not None
    return lease
