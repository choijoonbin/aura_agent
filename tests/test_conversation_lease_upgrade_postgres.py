from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from psycopg import connect
from psycopg.errors import ForeignKeyViolation, RestrictViolation

from dwp_agent.contracts import ConversationMessage, ConversationRole
from dwp_agent.conversation_store import ConversationNotFound
from dwp_agent.database_migrations import apply_migrations, migration_sort_key
from dwp_agent.governed_worker_runtime import GovernedWorkerMaintenance
from dwp_agent.postgres_conversation_store import PostgresConversationStore


DATABASE_URL = os.getenv("DWP_AGENT_LEASE_UPGRADE_TEST_DATABASE_URL", "").strip()
MIGRATION_ROOT = Path(__file__).resolve().parents[1] / "src" / "dwp_agent" / "migrations"
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_LEASE_UPGRADE_TEST_DATABASE_URL is not configured.",
)


class FixtureEncryption:
    prefix = "dwp2.upgrade-fixture."

    def encrypt_bytes(self, payload: bytes, *_args, **_kwargs) -> str:
        return self.prefix + base64.urlsafe_b64encode(payload).decode("ascii")

    def decrypt_bytes(self, *, envelope: str | None, **_kwargs) -> bytes:
        assert envelope is not None and envelope.startswith(self.prefix)
        return base64.urlsafe_b64decode(envelope.removeprefix(self.prefix))


@pytest.mark.integration
def test_legacy_database_upgrades_through_applied_v33_to_latest() -> None:
    _require_dedicated_database()
    _apply_migrations_through(17)
    tenant_id = str(910_000_000 + uuid4().int % 80_000_000)
    user_id = "legacy-upgrade-user"
    completed_run_id = uuid4()
    failed_run_id = uuid4()
    visible_conversation_id = uuid4()
    empty_conversation_id = uuid4()
    encryption = FixtureEncryption()

    _insert_legacy_fixture(
        tenant_id=tenant_id,
        user_id=user_id,
        completed_run_id=completed_run_id,
        failed_run_id=failed_run_id,
        visible_conversation_id=visible_conversation_id,
        empty_conversation_id=empty_conversation_id,
        encryption=encryption,
    )
    _apply_pending_migrations_through(33)
    with connect(DATABASE_URL) as connection:
        applied_v33 = connection.execute(
            "SELECT checksum FROM sys_schema_history WHERE version = 'V33'"
        ).fetchone()
    assert applied_v33 == (
        "b90332db6a11ae2b3fa91d2874146402f499d76f9665a89eea29ba15d58085a4",
    )
    with pytest.raises(RuntimeError, match="schema is unavailable"):
        GovernedWorkerMaintenance._require_database_schema(DATABASE_URL)
    apply_migrations(DATABASE_URL)
    GovernedWorkerMaintenance._require_database_schema(DATABASE_URL)

    with connect(DATABASE_URL) as connection:
        messages = connection.execute(
            """SELECT role, run_id, lease_generation
                 FROM ai_conversation_messages
                WHERE conversation_id = %s
                ORDER BY role DESC""",
            (visible_conversation_id,),
        ).fetchall()
        empty_count = connection.execute(
            """SELECT message_count FROM ai_conversations
                WHERE conversation_id = %s""",
            (empty_conversation_id,),
        ).fetchone()[0]
        nullability = dict(
            connection.execute(
                """SELECT column_name, is_nullable
                     FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'ai_conversation_messages'
                      AND column_name IN ('run_id', 'lease_generation')"""
            ).fetchall()
        )
        delete_rule = connection.execute(
            """SELECT delete_rule
                 FROM information_schema.referential_constraints
                WHERE constraint_schema = 'public'
                  AND constraint_name = 'ai_conversation_messages_run_id_fkey'"""
        ).fetchone()[0]
        versions = connection.execute(
            """SELECT version FROM sys_schema_history
                ORDER BY substring(version from 2)::int"""
        ).fetchall()

    assert messages == [
        ("USER", completed_run_id, 1),
        ("ASSISTANT", completed_run_id, 1),
    ]
    assert empty_count == 0
    assert nullability == {"lease_generation": "NO", "run_id": "NO"}
    assert delete_rule == "RESTRICT"
    expected_versions = [
        migration.stem.split("__", 1)[0]
        for migration in sorted(
            MIGRATION_ROOT.glob("V*__*.sql"), key=migration_sort_key
        )
    ]
    assert "V18" in expected_versions
    assert [row[0] for row in versions] == expected_versions

    store = PostgresConversationStore(DATABASE_URL, encryption)  # type: ignore[arg-type]
    summaries = store.list(tenant_id=tenant_id, user_id=user_id)
    assert [summary.conversation_id for summary in summaries] == [visible_conversation_id]
    detail = store.get(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=visible_conversation_id,
    )
    assert detail.summary.message_count == 2
    assert [message.content for message in detail.messages] == [
        "Legacy completed question",
        "Legacy completed answer",
    ]
    with pytest.raises(ConversationNotFound):
        store.get(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=empty_conversation_id,
        )

    connection = connect(DATABASE_URL)
    try:
        with pytest.raises((ForeignKeyViolation, RestrictViolation)):
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE run_id = %s",
                (completed_run_id,),
            )
        connection.rollback()
    finally:
        connection.close()


def _apply_migrations_through(maximum_version: int) -> None:
    migrations = sorted(MIGRATION_ROOT.glob("V*__*.sql"), key=migration_sort_key)
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """CREATE TABLE sys_schema_history (
                   version VARCHAR(40) PRIMARY KEY,
                   description VARCHAR(200) NOT NULL,
                   checksum CHAR(64) NOT NULL,
                   installed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        for migration in migrations:
            version, description = migration.stem.split("__", 1)
            if int(version.removeprefix("V")) > maximum_version:
                break
            sql = migration.read_text(encoding="utf-8")
            connection.execute(sql)
            connection.execute(
                """INSERT INTO sys_schema_history (version, description, checksum)
                   VALUES (%s, %s, %s)""",
                (
                    version,
                    description.replace("_", " "),
                    hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                ),
            )


def _apply_pending_migrations_through(maximum_version: int) -> None:
    migrations = sorted(MIGRATION_ROOT.glob("V*__*.sql"), key=migration_sort_key)
    with connect(DATABASE_URL) as connection:
        applied = {
            row[0]
            for row in connection.execute("SELECT version FROM sys_schema_history")
        }
        for migration in migrations:
            version, description = migration.stem.split("__", 1)
            if int(version.removeprefix("V")) > maximum_version:
                break
            if version in applied:
                continue
            sql = migration.read_text(encoding="utf-8")
            connection.execute(sql)
            connection.execute(
                """INSERT INTO sys_schema_history (version, description, checksum)
                   VALUES (%s, %s, %s)""",
                (
                    version,
                    description.replace("_", " "),
                    hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                ),
            )


def _insert_legacy_fixture(
    *,
    tenant_id: str,
    user_id: str,
    completed_run_id: UUID,
    failed_run_id: UUID,
    visible_conversation_id: UUID,
    empty_conversation_id: UUID,
    encryption: FixtureEncryption,
) -> None:
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """INSERT INTO ai_conversation_retention_policies (tenant_id, retention_days)
               VALUES (%s, 90)""",
            (int(tenant_id),),
        )
        _insert_run(
            connection,
            run_id=completed_run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            request_id="legacy-completed",
            state="COMPLETED",
        )
        _insert_run(
            connection,
            run_id=failed_run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            request_id="legacy-failed",
            state="FAILED",
        )
        for conversation_id, title, count in (
            (visible_conversation_id, "Visible legacy conversation", 6),
            (empty_conversation_id, "Hidden legacy conversation", 2),
        ):
            connection.execute(
                """INSERT INTO ai_conversations (
                       conversation_id, tenant_id, user_id, locale,
                       title_envelope, message_count, retention_until)
                   VALUES (%s, %s, %s, 'en', %s, %s,
                           CURRENT_TIMESTAMP + INTERVAL '90 days')""",
                (
                    conversation_id,
                    int(tenant_id),
                    user_id,
                    encryption.encrypt_bytes(title.encode("utf-8")),
                    count,
                ),
            )

        _insert_exchange(
            connection,
            encryption=encryption,
            conversation_id=visible_conversation_id,
            request_id="legacy-completed",
            assistant_run_id=completed_run_id,
            user_content="Legacy completed question",
            assistant_content="Legacy completed answer",
        )
        _insert_exchange(
            connection,
            encryption=encryption,
            conversation_id=visible_conversation_id,
            request_id="legacy-failed",
            assistant_run_id=failed_run_id,
            user_content="Legacy failed question",
            assistant_content="Legacy failed answer",
        )
        _insert_exchange(
            connection,
            encryption=encryption,
            conversation_id=visible_conversation_id,
            request_id="legacy-orphan",
            assistant_run_id=None,
            user_content="Legacy orphan question",
            assistant_content="Legacy orphan answer",
        )
        _insert_exchange(
            connection,
            encryption=encryption,
            conversation_id=empty_conversation_id,
            request_id="empty-orphan",
            assistant_run_id=None,
            user_content="Hidden orphan question",
            assistant_content="Hidden orphan answer",
        )


def _insert_run(
    connection,
    *,
    run_id: UUID,
    tenant_id: str,
    user_id: str,
    request_id: str,
    state: str,
) -> None:
    connection.execute(
        """INSERT INTO ai_agent_runs (
               run_id, tenant_id, user_id, request_id, query_hash, agent_key,
               agent_revision, run_state, answer_state, risk_tier, policy_outcome,
               status_code, locale, correlation_id, completed_at)
           VALUES (%s, %s, %s, %s, %s, 'DWP_ASSISTANT', 1, %s, %s,
                   'L1', 'ALLOW', %s, 'en', %s, CURRENT_TIMESTAMP)""",
        (
            run_id,
            int(tenant_id),
            user_id,
            request_id,
            hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
            state,
            "COMPLETED" if state == "COMPLETED" else None,
            "ANSWER_GROUNDED" if state == "COMPLETED" else None,
            f"upgrade-{request_id}",
        ),
    )


def _insert_exchange(
    connection,
    *,
    encryption: FixtureEncryption,
    conversation_id: UUID,
    request_id: str,
    assistant_run_id: UUID | None,
    user_content: str,
    assistant_content: str,
) -> None:
    created_at = datetime.now(timezone.utc)
    for role, content, run_id in (
        (ConversationRole.USER, user_content, None),
        (ConversationRole.ASSISTANT, assistant_content, assistant_run_id),
    ):
        message_created_at = created_at + (
            timedelta(microseconds=1) if role == ConversationRole.ASSISTANT else timedelta()
        )
        message = ConversationMessage(
            message_id=uuid4(),
            role=role,
            content=content,
            run_id=run_id,
            status_code="ANSWER_GROUNDED" if role == ConversationRole.ASSISTANT else None,
            created_at=message_created_at,
        )
        connection.execute(
            """INSERT INTO ai_conversation_messages (
                   message_id, conversation_id, request_id, run_id, role,
                   payload_envelope, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                message.message_id,
                conversation_id,
                request_id,
                run_id,
                role,
                encryption.encrypt_bytes(
                    message.model_dump_json(by_alias=True).encode("utf-8")
                ),
                message_created_at,
            ),
        )


def _require_dedicated_database() -> None:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith("_verify"):
        pytest.fail("Conversation lease upgrade tests require a dedicated verify database.")
