from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.conversation_store import ConversationStoreUnavailable
from dwp_agent.postgres_conversation_store import PostgresConversationStore
from dwp_agent.run_store import _apply_migrations


class EncryptionStub:
    def encrypt_bytes(self, *_args, **_kwargs) -> str:
        return "dwp2.test"


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
