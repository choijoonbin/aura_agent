from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent import postgres_conversation_store as postgres_store_module
from dwp_agent.conversation_store import ConversationStoreUnavailable
from dwp_agent.contracts import (
    AskCitation,
    CitationSourceType,
    ConversationMessage,
    ConversationRole,
)
from dwp_agent.postgres_conversation_store import PostgresConversationStore
from dwp_agent.run_store import _apply_migrations


class EncryptionStub:
    def encrypt_bytes(self, *_args, **_kwargs) -> str:
        return "dwp2.test"


class SummaryEncryptionStub:
    def __init__(self, message: ConversationMessage) -> None:
        self.message = message

    def decrypt_bytes(self, *, envelope: str | None, **_kwargs) -> bytes:
        if envelope == "dwp2.summary-title":
            return "저장된 검토 대화".encode()
        if envelope == "dwp2.summary-message":
            return self.message.model_dump_json(by_alias=True).encode()
        raise AssertionError(f"Unexpected encrypted envelope: {envelope}")


class SummaryQueryResult:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple]:
        return self.rows


class SummaryConnection:
    def __init__(self, row: tuple) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> SummaryQueryResult:
        self.calls.append((sql, params))
        return SummaryQueryResult([self.row])


def test_postgres_list_decrypts_latest_completed_answer_metadata_and_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    conversation_id = uuid4()
    message = ConversationMessage(
        message_id=uuid4(),
        role=ConversationRole.ASSISTANT,
        content="  실제 저장 답변을\n요약합니다.  ",
        run_id=uuid4(),
        status_code="ANSWER_GROUNDED",
        citations=[
            AskCitation(
                source_id="src-01",
                source_type=CitationSourceType.MAIL,
                title="검토 메일",
                source_system="DWP Mail",
            ),
            AskCitation(
                source_id="src-02",
                source_type=CitationSourceType.MAIL,
                title="후속 메일",
                source_system="DWP Mail",
            ),
        ],
        agent_key="DWP_ASSISTANT",
        created_at=now,
    )
    retention_until = now + timedelta(days=90)
    row = (
        conversation_id,
        "ko-KR",
        2,
        now,
        now,
        now,
        "dwp2.summary-title",
        None,
        None,
        None,
        retention_until,
        True,
        message.message_id,
        message.role,
        "dwp2.summary-message",
        None,
        None,
        None,
        "OPENAI",
    )
    connection = SummaryConnection(row)
    monkeypatch.setattr(postgres_store_module, "connect", lambda _url: connection)
    store = PostgresConversationStore(
        "postgresql://summary-test",
        SummaryEncryptionStub(message),  # type: ignore[arg-type]
    )

    summaries = store.list(tenant_id="42", user_id="member-1")

    assert len(summaries) == 1
    assert summaries[0].model_dump(by_alias=True) == {
        "conversationId": conversation_id,
        "title": "저장된 검토 대화",
        "locale": "ko-KR",
        "messageCount": 2,
        "agentKey": "DWP_ASSISTANT",
        "sourceSystems": ["DWP Mail"],
        "evidenceCount": 2,
        "summaryExcerpt": "실제 저장 답변을 요약합니다.",
        "lastAnswerStatus": "ANSWER_GROUNDED",
        "retentionUntil": retention_until,
        "legalHold": True,
        "createdAt": now,
        "updatedAt": now,
        "lastMessageAt": now,
    }
    sql, params = connection.calls[0]
    assert "completed_run.tenant_id = conversation.tenant_id" in sql
    assert "completed_run.user_id = conversation.user_id" in sql
    assert "completed_run.request_id = message.request_id" in sql
    assert params == (42, "member-1", 30)


@pytest.mark.integration
def test_expired_conversation_read_filters_without_deleting() -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Conversation integration tests require a dedicated test database.")

    _apply_migrations(database_url)
    tenant_id = 900_000_000 + uuid4().int % 100_000_000
    conversation_id = uuid4()
    with connect(database_url) as connection:
        connection.execute(
            """INSERT INTO ai_conversations (
                   conversation_id, tenant_id, user_id, locale,
                   title_envelope, retention_until)
               VALUES (%s, %s, 'expired-user', 'ko-KR', 'dwp2.test',
                       CURRENT_TIMESTAMP - INTERVAL '1 day')""",
            (conversation_id, tenant_id),
        )

    store = PostgresConversationStore(
        database_url=database_url,
        encryption=None,  # type: ignore[arg-type]
    )
    assert store.list(tenant_id=str(tenant_id), user_id="expired-user") == []

    with connect(database_url) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM ai_conversations WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()[0]
    assert remaining == 1


@pytest.mark.integration
def test_conversation_creation_requires_explicit_retention_bootstrap() -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Conversation integration tests require a dedicated test database.")
    _apply_migrations(database_url)
    tenant_id = 900_000_000 + uuid4().int % 100_000_000
    store = PostgresConversationStore(database_url, EncryptionStub())  # type: ignore[arg-type]

    with pytest.raises(ConversationStoreUnavailable, match="explicit bootstrap"):
        store.ensure(
            tenant_id=str(tenant_id),
            user_id="unconfigured-user",
            conversation_id=None,
            locale="ko-KR",
            initial_query="This conversation must not create policy defaults.",
        )

    with connect(database_url) as connection:
        row = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_conversation_retention_policies
                     WHERE tenant_id = %s),
                   (SELECT COUNT(*) FROM ai_conversations WHERE tenant_id = %s)""",
            (tenant_id, tenant_id),
        ).fetchone()
    assert row == (0, 0)
