from __future__ import annotations

from uuid import UUID

from .envelope import KeyContext


def run_context(tenant_id: str, run_id: str) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="agent-run",
        resource_id=run_id,
        field="response",
    )


def legacy_run_aad(
    tenant_id: str, user_id: str, request_id: str, run_id: str
) -> bytes:
    return f"{tenant_id}:{user_id}:{request_id}:{run_id}".encode("utf-8")


def conversation_context(
    tenant_id: str, conversation_id: UUID, field: str
) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="conversation",
        resource_id=str(conversation_id),
        field=field,
    )


def legacy_conversation_aad(
    tenant_id: str, user_id: str, conversation_id: UUID, field: str
) -> bytes:
    return f"{tenant_id}:{user_id}:{conversation_id}:{field}".encode()


def message_context(
    tenant_id: str, conversation_id: UUID, message_id: UUID
) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="conversation-message",
        resource_id=f"{conversation_id}:{message_id}",
        field="payload",
    )


def legacy_message_aad(message_id: UUID) -> bytes:
    return f"dwaion-message:{message_id}".encode()


def feedback_context(tenant_id: str, run_id: UUID) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="agent-feedback",
        resource_id=str(run_id),
        field="comment",
    )


def evaluation_context(
    tenant_id: str, evaluation_set_id: UUID, evaluation_case_id: UUID, field: str
) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="evaluation-case",
        resource_id=f"{evaluation_set_id}:{evaluation_case_id}",
        field=field,
    )


def legacy_evaluation_aad(
    tenant_id: str, evaluation_set_id: UUID, evaluation_case_id: UUID, field: str
) -> bytes:
    return (
        f"dwaion:evaluation:{tenant_id}:{evaluation_set_id}:{evaluation_case_id}:{field}"
    ).encode("utf-8")
