from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.meeting_media_security import (
    MeetingMediaPurpose,
    PostgresMeetingMediaReplayStore,
)


@pytest.mark.integration
def test_postgres_replay_evidence_is_durable_purpose_scoped_and_content_free() -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    schema = "test_meeting_media_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in database_url else "?"
    isolated_url = (
        database_url + separator + "options=" + quote(f"-csearch_path={schema}")
    )
    apply_migrations(isolated_url)
    store = PostgresMeetingMediaReplayStore(isolated_url)
    jti = uuid4()
    values = dict(
        scope="RESOURCE",
        jti=jti,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        tenant_id=77,
        meeting_id=UUID("29f14739-0f92-469e-8528-d3731e809f55"),
        resource_id=UUID("776d0d8e-96e2-4c29-8df5-9f5e3beaf72f"),
        body_sha256="a" * 64,
    )
    try:
        assert store.consume(purpose=MeetingMediaPurpose.RECORDING, **values) is True
        assert store.consume(purpose=MeetingMediaPurpose.RECORDING, **values) is False
        assert store.consume(purpose=MeetingMediaPurpose.TRANSCRIPT, **values) is True
        with psycopg.connect(isolated_url) as connection:
            columns = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'agent_meeting_media_assertion_replay'
                    """
                ).fetchall()
            }
        assert not {
            "transcript",
            "object_key",
            "access_url",
            "service_token",
            "participant_name",
        }.intersection(columns)
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
