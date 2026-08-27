from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.envelope import load_payload_encryption
from dwp_agent.question_launch_store import (
    MAX_ACTIVE_LAUNCHES,
    MAX_CREATES_PER_MINUTE,
    PostgresQuestionLaunchStore,
    QuestionLaunchCapacityExceeded,
    QuestionLaunchNotFound,
    QuestionLaunchUnavailable,
)
from dwp_agent.run_store import _apply_migrations


DATABASE_URL = os.getenv("DWP_AGENT_ENVELOPE_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_ENVELOPE_TEST_DATABASE_URL is not configured.",
)


@pytest.mark.integration
def test_question_launch_is_encrypted_session_bound_and_consumed_once() -> None:
    _require_dedicated_database()
    _apply_migrations(DATABASE_URL)
    store = PostgresQuestionLaunchStore(DATABASE_URL, load_payload_encryption())
    tenant_id = str(860_000_000 + uuid4().int % 100_000_000)
    question = "Confidential acquisition planning question"
    launch = store.create(
        tenant_id=tenant_id,
        user_id="launch-user",
        session_family_id="session-a",
        question=question,
    )

    try:
        with connect(DATABASE_URL) as connection:
            envelope = connection.execute(
                "SELECT question_envelope FROM ai_question_launch_tickets WHERE launch_id = %s",
                (launch.launch_id,),
            ).fetchone()[0]
        assert envelope.startswith("dwp2.")
        assert question not in envelope

        for boundary in (
            {"tenant_id": str(int(tenant_id) + 1), "user_id": "launch-user", "session_family_id": "session-a"},
            {"tenant_id": tenant_id, "user_id": "other-user", "session_family_id": "session-a"},
            {"tenant_id": tenant_id, "user_id": "launch-user", "session_family_id": "session-b"},
        ):
            with pytest.raises(QuestionLaunchNotFound):
                store.consume(launch_id=launch.launch_id, **boundary)

        barrier = Barrier(3)

        def consume_once() -> str | None:
            barrier.wait()
            try:
                return store.consume(
                    tenant_id=tenant_id,
                    user_id="launch-user",
                    session_family_id="session-a",
                    launch_id=launch.launch_id,
                )
            except QuestionLaunchNotFound:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = [executor.submit(consume_once) for _ in range(2)]
            barrier.wait()
            results = [attempt.result() for attempt in attempts]

        assert results.count(question) == 1
        assert results.count(None) == 1
        with pytest.raises(QuestionLaunchNotFound):
            store.consume(
                tenant_id=tenant_id,
                user_id="launch-user",
                session_family_id="session-a",
                launch_id=launch.launch_id,
            )
    finally:
        with connect(DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM ai_question_launch_tickets WHERE tenant_id = %s", (int(tenant_id),)
            )
            connection.execute(
                "DELETE FROM ai_question_launch_rate_events WHERE tenant_id = %s", (int(tenant_id),)
            )


@pytest.mark.integration
def test_question_launch_enforces_ttl_capacity_rate_and_maintenance() -> None:
    _require_dedicated_database()
    _apply_migrations(DATABASE_URL)
    store = PostgresQuestionLaunchStore(DATABASE_URL, load_payload_encryption())
    tenant_id = str(870_000_000 + uuid4().int % 100_000_000)

    try:
        active = [
            store.create(
                tenant_id=tenant_id,
                user_id="capacity-user",
                session_family_id="capacity-session",
                question=f"Capacity question {index}",
            )
            for index in range(MAX_ACTIVE_LAUNCHES)
        ]
        with pytest.raises(QuestionLaunchCapacityExceeded):
            store.create(
                tenant_id=tenant_id,
                user_id="capacity-user",
                session_family_id="capacity-session",
                question="One launch too many",
            )
        with connect(DATABASE_URL) as connection:
            lifetime = connection.execute(
                """SELECT EXTRACT(EPOCH FROM expires_at - created_at)::int
                     FROM ai_question_launch_tickets WHERE launch_id = %s""",
                (active[0].launch_id,),
            ).fetchone()[0]
        assert lifetime == 60

        for index in range(MAX_CREATES_PER_MINUTE):
            launch = store.create(
                tenant_id=tenant_id,
                user_id="rate-user",
                session_family_id="rate-session",
                question=f"Rate question {index}",
            )
            assert store.consume(
                tenant_id=tenant_id,
                user_id="rate-user",
                session_family_id="rate-session",
                launch_id=launch.launch_id,
            ) == f"Rate question {index}"
        with pytest.raises(QuestionLaunchCapacityExceeded):
            store.create(
                tenant_id=tenant_id,
                user_id="rate-user",
                session_family_id="rate-session",
                question="One request too many",
            )

        expiring = store.create(
            tenant_id=tenant_id,
            user_id="cleanup-user",
            session_family_id="cleanup-session",
            question="Expired question",
        )
        with connect(DATABASE_URL) as connection:
            connection.execute(
                """UPDATE ai_question_launch_tickets
                      SET created_at = CURRENT_TIMESTAMP - INTERVAL '120 seconds',
                          expires_at = CURRENT_TIMESTAMP - INTERVAL '60 seconds'
                    WHERE launch_id = %s""",
                (expiring.launch_id,),
            )
            connection.execute(
                """UPDATE ai_question_launch_rate_events
                      SET created_at = CURRENT_TIMESTAMP - INTERVAL '6 minutes'
                    WHERE tenant_id = %s AND user_id = 'cleanup-user'""",
                (int(tenant_id),),
            )
        deleted_tickets, deleted_rates = store.delete_expired()
        assert deleted_tickets >= 1
        assert deleted_rates >= 1
    finally:
        with connect(DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM ai_question_launch_tickets WHERE tenant_id = %s", (int(tenant_id),)
            )
            connection.execute(
                "DELETE FROM ai_question_launch_rate_events WHERE tenant_id = %s", (int(tenant_id),)
            )


@pytest.mark.integration
def test_question_launch_remains_consumed_when_envelope_decryption_fails() -> None:
    _require_dedicated_database()
    _apply_migrations(DATABASE_URL)
    store = PostgresQuestionLaunchStore(DATABASE_URL, load_payload_encryption())
    tenant_id = str(880_000_000 + uuid4().int % 100_000_000)
    launch = store.create(
        tenant_id=tenant_id,
        user_id="tamper-user",
        session_family_id="tamper-session",
        question="Tamper evidence",
    )

    try:
        with connect(DATABASE_URL) as connection:
            connection.execute(
                """UPDATE ai_question_launch_tickets SET question_envelope = 'dwp2.invalid'
                    WHERE launch_id = %s""",
                (launch.launch_id,),
            )
        with pytest.raises(QuestionLaunchUnavailable):
            store.consume(
                tenant_id=tenant_id,
                user_id="tamper-user",
                session_family_id="tamper-session",
                launch_id=launch.launch_id,
            )
        with pytest.raises(QuestionLaunchNotFound):
            store.consume(
                tenant_id=tenant_id,
                user_id="tamper-user",
                session_family_id="tamper-session",
                launch_id=launch.launch_id,
            )
    finally:
        with connect(DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM ai_question_launch_tickets WHERE tenant_id = %s", (int(tenant_id),)
            )
            connection.execute(
                "DELETE FROM ai_question_launch_rate_events WHERE tenant_id = %s", (int(tenant_id),)
            )


def _require_dedicated_database() -> None:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Question launch tests require a dedicated test database.")
