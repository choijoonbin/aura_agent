from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dwp_agent import artifact_api
from dwp_agent.artifact_contracts import GovernedArtifact
from dwp_agent.contracts import (
    AskCitation,
    ConversationDetail,
    ConversationMessage,
    ConversationRole,
    ConversationSummary,
)
from dwp_agent.conversation_store import (
    ConversationNotFound,
    ConversationStoreUnavailable,
)


CONVERSATION_ID = UUID("c00677f9-251d-4a0d-bd8c-85db5dc2ca93")
ASSISTANT_MESSAGE_ID = UUID("a14a615a-8a24-48dd-b45e-dc3eaed3dcf1")
USER_MESSAGE_ID = UUID("530a52a2-385c-4167-89ef-85ecbd55c86d")
ARTIFACT_ID = UUID("4053a568-7bd0-4bd9-a39c-ee0d6e12e51a")
ANSWER = "Grounded answer preserved exactly for governed review."


class _ArtifactStore:
    def __init__(self) -> None:
        self.requests = []

    def create(self, _identity, request):
        self.requests.append(request)
        now = datetime.now(UTC)
        return GovernedArtifact(
            artifact_id=ARTIFACT_ID,
            artifact_type=request.artifact_type,
            state="DRAFT",
            revision=1,
            draft_revision=1,
            current_version_number=0,
            content=request.content,
            sources=request.sources,
            created_at=now,
            updated_at=now,
        )


class _ConversationStore:
    def __init__(
        self,
        conversation: ConversationDetail | None = None,
        error: Exception | None = None,
    ) -> None:
        self.conversation = conversation
        self.error = error
        self.calls: list[tuple[str, str, UUID]] = []

    def get(self, *, tenant_id: str, user_id: str, conversation_id: UUID):
        self.calls.append((tenant_id, user_id, conversation_id))
        if self.error is not None:
            raise self.error
        assert self.conversation is not None
        return self.conversation


def _conversation(
    *,
    status_code: str = "ANSWER_GROUNDED",
    citations: list[AskCitation] | None = None,
    include_assistant: bool = True,
) -> ConversationDetail:
    now = datetime.now(UTC)
    resolved_citations = citations
    if resolved_citations is None:
        resolved_citations = [
            AskCitation(
                source_id="src-01",
                source_type="WORK_ITEM",
                title="Work item",
                source_system="DWP_WORK",
            ),
            AskCitation(
                source_id="src-02",
                source_type="MAIL",
                title="Mail message",
                source_system="DWP_MAIL",
            ),
        ]
    messages = [
        ConversationMessage(
            message_id=USER_MESSAGE_ID,
            role=ConversationRole.USER,
            content="Prepare the review.",
            created_at=now,
        )
    ]
    if include_assistant:
        messages.append(
            ConversationMessage(
                message_id=ASSISTANT_MESSAGE_ID,
                role=ConversationRole.ASSISTANT,
                content=ANSWER,
                status_code=status_code,
                citations=resolved_citations,
                created_at=now,
            )
        )
    return ConversationDetail(
        summary=ConversationSummary(
            conversation_id=CONVERSATION_ID,
            title="Governed review",
            locale="en",
            message_count=len(messages),
            agent_key="DWP_ASSISTANT" if include_assistant else None,
            source_systems=(
                list(dict.fromkeys(item.source_system for item in resolved_citations))
                if include_assistant
                else []
            ),
            evidence_count=len(resolved_citations) if include_assistant else 0,
            summary_excerpt=ANSWER if include_assistant else None,
            last_answer_status=status_code if include_assistant else None,
            retention_until=now,
            legal_hold=False,
            created_at=now,
            updated_at=now,
            last_message_at=now,
        ),
        messages=messages,
    )


def _payload(*, body: str = ANSWER) -> dict[str, object]:
    return {
        "commandId": "95029013-b2d2-4b03-b7ed-fc6e997c58f1",
        "expectedRevision": 0,
        "reasonCode": "USER_ARTIFACT_CREATE",
        "artifactType": "DOCUMENT",
        "content": {"title": "Review", "body": body},
        "sourceConversation": {
            "conversationId": str(CONVERSATION_ID),
            "assistantMessageId": str(ASSISTANT_MESSAGE_ID),
        },
    }


def _headers() -> dict[str, str]:
    return {
        "X-DWP-Service-Token": "personal-domain-service-token",
        "X-DWP-Tenant-ID": "7001",
        "X-DWP-User-ID": "member-1",
        "X-Correlation-ID": "artifact-conversation-source-test",
        "X-DWP-Auth-Session-ID": "session-1",
        "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Access-Mode": "NORMAL",
        "X-DWP-Roles": "WORKSPACE_MEMBER",
        "X-DWP-Permissions": ",".join(
            (
                "APP.ASK:VIEW",
                "APP.DWAION_ARTIFACTS:CREATE",
                "APP.WORK:VIEW",
                "APP.MAIL:VIEW",
            )
        ),
    }


def _client(
    monkeypatch: pytest.MonkeyPatch,
    conversation_store: _ConversationStore,
) -> tuple[TestClient, _ArtifactStore]:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", "personal-domain-service-token")
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    artifacts = _ArtifactStore()
    monkeypatch.setattr(artifact_api, "get_artifact_store", lambda: artifacts)
    monkeypatch.setattr(
        artifact_api, "get_conversation_store", lambda: conversation_store
    )
    app = FastAPI()
    app.include_router(artifact_api.router)
    return TestClient(app), artifacts


@pytest.mark.parametrize(
    "status_code",
    ("ANSWER_GROUNDED", "ANSWER_GROUNDED_FALLBACK"),
)
def test_create_derives_sources_from_scoped_grounded_assistant_message(
    monkeypatch: pytest.MonkeyPatch,
    status_code: str,
) -> None:
    conversations = _ConversationStore(_conversation(status_code=status_code))
    http, artifacts = _client(monkeypatch, conversations)

    response = http.post("/v1/artifacts", headers=_headers(), json=_payload())

    assert response.status_code == 201
    assert conversations.calls == [("7001", "member-1", CONVERSATION_ID)]
    assert len(artifacts.requests) == 1
    stored_request = artifacts.requests[0]
    assert stored_request.source_conversation is None
    assert stored_request._verified_source_references == frozenset(
        source.reference for source in stored_request.sources
    )
    stored_sources = [
        source.model_dump(mode="json", by_alias=True)
        for source in stored_request.sources
    ]
    assert stored_sources == [
        {
            "sourceType": "WORK_ITEM",
            "reference": (
                f"conversation:{CONVERSATION_ID}:message:{ASSISTANT_MESSAGE_ID}:"
                "citation:src-01"
            ),
        },
        {
            "sourceType": "MAIL",
            "reference": (
                f"conversation:{CONVERSATION_ID}:message:{ASSISTANT_MESSAGE_ID}:"
                "citation:src-02"
            ),
        },
    ]


def test_create_rejects_client_sources_with_conversation_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversations = _ConversationStore(_conversation())
    http, artifacts = _client(monkeypatch, conversations)
    payload = _payload()
    payload["sources"] = [
        {"sourceType": "WORK_ITEM", "reference": "client-controlled-reference"}
    ]

    response = http.post("/v1/artifacts", headers=_headers(), json=payload)

    assert response.status_code == 422
    assert conversations.calls == []
    assert artifacts.requests == []


def test_create_rejects_a_conversation_result_outside_the_bound_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = _conversation()
    conversation = conversation.model_copy(
        update={
            "summary": conversation.summary.model_copy(
                update={
                    "conversation_id": UUID("62153590-42d6-4c5d-9127-45e356d5f422")
                }
            )
        }
    )
    http, artifacts = _client(monkeypatch, _ConversationStore(conversation))

    response = http.post("/v1/artifacts", headers=_headers(), json=_payload())

    assert response.status_code == 404
    assert artifacts.requests == []


@pytest.mark.parametrize(
    ("conversation", "body", "expected_status"),
    (
        (_conversation(status_code="ANSWER_ABSTAINED"), ANSWER, 409),
        (_conversation(citations=[]), ANSWER, 409),
        (_conversation(), "Edited or substituted content.", 409),
        (_conversation(include_assistant=False), ANSWER, 404),
    ),
)
def test_create_fails_closed_when_bound_answer_is_not_exactly_grounded(
    monkeypatch: pytest.MonkeyPatch,
    conversation: ConversationDetail,
    body: str,
    expected_status: int,
) -> None:
    conversations = _ConversationStore(conversation)
    http, artifacts = _client(monkeypatch, conversations)

    response = http.post(
        "/v1/artifacts", headers=_headers(), json=_payload(body=body)
    )

    assert response.status_code == expected_status
    assert artifacts.requests == []


@pytest.mark.parametrize(
    ("error", "expected_status"),
    (
        (
            ConversationNotFound(
                "Conversation was not found in the verified user scope."
            ),
            404,
        ),
        (ConversationStoreUnavailable("Database is unavailable."), 503),
    ),
)
def test_create_maps_scoped_conversation_lookup_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
) -> None:
    conversations = _ConversationStore(error=error)
    http, artifacts = _client(monkeypatch, conversations)

    response = http.post("/v1/artifacts", headers=_headers(), json=_payload())

    assert response.status_code == expected_status
    assert artifacts.requests == []
    if expected_status == 503:
        assert response.json()["detail"] == "Governed artifacts are unavailable."
