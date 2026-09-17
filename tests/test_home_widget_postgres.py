from __future__ import annotations

import base64
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.artifact_home_projection import (
    _BACKFILL_SELECT,
    PostgresArtifactHomeProjectionBackfill,
    validate_artifact_home_projection_activation,
)
from dwp_agent.database_migrations import apply_migrations
from dwp_agent.governed_domain_core import GovernedPayloadCodec
from dwp_agent.home_widget_identity import PostgresHomeIdentityReplayStore
from dwp_agent.personal_domain_security import PersonalDomainIdentity


DATABASE_URL = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Home provider integration tests require a dedicated test database.")
    names = (
        "DWP_ENVIRONMENT",
        "DWP_AGENT_KEY_PROVIDER",
        "DWP_AGENT_DATA_KEY_VERSION",
        "DWP_AGENT_DATA_KEY",
        "DWP_AGENT_DATABASE_URL",
        "DWP_DWAION_HOME_TITLE_PROJECTION_READY",
    )
    previous = {name: os.environ.get(name) for name in names}
    initialized = False
    try:
        os.environ["DWP_ENVIRONMENT"] = "local"
        os.environ["DWP_AGENT_KEY_PROVIDER"] = "local-inline"
        os.environ["DWP_AGENT_DATA_KEY_VERSION"] = "home-test-v1"
        os.environ["DWP_AGENT_DATA_KEY"] = base64.b64encode(b"h" * 32).decode("ascii")
        apply_migrations(DATABASE_URL)
        initialized = True
        _reset()
        yield
    finally:
        if initialized:
            _reset()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def restore_per_test_home_environment() -> None:
    names = (
        "DWP_AGENT_DATABASE_URL",
        "DWP_DWAION_HOME_TITLE_PROJECTION_READY",
    )
    previous = {name: os.environ.get(name) for name in names}
    yield
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_v41_migration_and_postgres_replay_admission_are_enforced() -> None:
    with connect(DATABASE_URL) as connection:
        migrations = connection.execute(
            "SELECT version, checksum FROM sys_schema_history "
            "WHERE version IN ('V41', 'V42') ORDER BY version"
        ).fetchall()
        relations = connection.execute(
            """SELECT to_regclass('agent_home_identity_assertion_replay'),
                      to_regclass('agent_artifact_home_projection_backfill_receipts'),
                      to_regclass('idx_ai_artifact_drafts_home_title_pending')"""
        ).fetchone()
        column = connection.execute(
            """SELECT is_nullable FROM information_schema.columns
                 WHERE table_name = 'ai_artifact_drafts'
                   AND column_name = 'home_title_envelope'"""
        ).fetchone()
    assert [row[0] for row in migrations] == ["V41", "V42"]
    assert all(len(row[1]) == 64 for row in migrations)
    assert relations == (
        "agent_home_identity_assertion_replay",
        "agent_artifact_home_projection_backfill_receipts",
        "idx_ai_artifact_drafts_home_title_pending",
    )
    assert column == ("YES",)

    with connect(DATABASE_URL) as connection:
        connection.execute("SET LOCAL enable_seqscan = off")
        plan = connection.execute(
            "EXPLAIN (FORMAT JSON) " + _BACKFILL_SELECT,
            (100,),
        ).fetchone()[0]
    assert "idx_ai_artifact_drafts_home_title_pending" in str(plan)

    store = PostgresHomeIdentityReplayStore(DATABASE_URL)
    jti = uuid4()
    values = {
        "jti": jti,
        "tenant_id": 7101,
        "user_id": "8201",
        "body_sha256": "a" * 64,
        "expires_at": datetime.now(UTC) + timedelta(seconds=30),
    }
    assert store.consume(**values) is True
    assert store.consume(**values) is False

    expired = uuid4()
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """INSERT INTO agent_home_identity_assertion_replay
                   (jti, tenant_id, user_id, body_sha256, consumed_at, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                expired,
                7101,
                "8201",
                "b" * 64,
                datetime.now(UTC) - timedelta(minutes=2),
                datetime.now(UTC) - timedelta(minutes=1),
            ),
        )
    assert store.consume(
        jti=uuid4(), tenant_id=7101, user_id="8201", body_sha256="c" * 64,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    ) is True
    with connect(DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT 1 FROM agent_home_identity_assertion_replay WHERE jti = %s",
            (expired,),
        ).fetchone() is None

    concurrent_jti = uuid4()
    barrier = Barrier(2)

    def consume_once() -> bool:
        barrier.wait()
        return store.consume(
            jti=concurrent_jti, tenant_id=7101, user_id="8201",
            body_sha256="e" * 64,
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume_once(), range(2)))
    assert sorted(results) == [False, True]


def test_bounded_backfill_activation_and_recipient_projection() -> None:
    _reset()
    codec = GovernedPayloadCodec()
    tenant_id, user_id = 7102, "8202"
    visible_id = _insert_artifact(
        codec, tenant_id=tenant_id, user_id=user_id, title="전략 초안",
        body="home must never decrypt or return this body",
    )
    _insert_artifact(
        codec, tenant_id=tenant_id, user_id="different-user", title="다른 사용자",
        body="must remain recipient isolated",
    )
    _insert_artifact(
        codec, tenant_id=tenant_id, user_id=user_id, title="보관됨", body="hidden",
        state="ARCHIVED",
    )

    backfill = PostgresArtifactHomeProjectionBackfill(DATABASE_URL)
    before = backfill.coverage()
    assert before.pending == 3 and before.succeeded == 0
    os.environ["DWP_AGENT_DATABASE_URL"] = DATABASE_URL
    os.environ["DWP_DWAION_HOME_TITLE_PROJECTION_READY"] = "true"
    with pytest.raises(RuntimeError, match="complete backfill coverage"):
        validate_artifact_home_projection_activation()

    assert backfill.process_batch(limit=2) == 2
    assert backfill.process_batch(limit=2) == 1
    assert backfill.coverage().complete is True
    validate_artifact_home_projection_activation()

    from dwp_agent.artifact_postgres_store import PostgresArtifactStore

    store = PostgresArtifactStore(DATABASE_URL)
    projection = store.home_projection(_identity(tenant_id, user_id), limit=50)
    assert projection.visible_count == 1
    assert len(projection.slots) == 1
    assert projection.slots[0] is not None
    assert projection.slots[0].artifact_id == visible_id
    assert projection.slots[0].title == "전략 초안"
    assert store.home_projection(
        _identity(tenant_id, "missing-user"), limit=50
    ).visible_count == 0


def test_corrupt_legacy_rows_have_bounded_retry_and_block_activation() -> None:
    _reset()
    codec = GovernedPayloadCodec()
    artifact_id = _insert_artifact(
        codec, tenant_id=7103, user_id="8203", title="손상 표본", body="hidden"
    )
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE ai_artifact_drafts SET content_envelope = 'dwp2.invalid' "
            "WHERE artifact_id = %s",
            (artifact_id,),
        )
    backfill = PostgresArtifactHomeProjectionBackfill(DATABASE_URL)
    assert backfill.process_batch(limit=10) == 1
    coverage = backfill.coverage()
    assert coverage.pending == 1 and coverage.failed == 1
    # The five-minute receipt cooldown prevents a refresh loop over the same corrupt row.
    assert backfill.process_batch(limit=10) == 0
    with connect(DATABASE_URL) as connection:
        receipt = connection.execute(
            """SELECT state, safe_error_code, attempt_count
                 FROM agent_artifact_home_projection_backfill_receipts
                WHERE artifact_id = %s""",
            (artifact_id,),
        ).fetchone()
    assert receipt == (
        "FAILED", "ARTIFACT_HOME_TITLE_PROJECTION_FAILED", 1
    )
    os.environ["DWP_AGENT_DATABASE_URL"] = DATABASE_URL
    os.environ["DWP_DWAION_HOME_TITLE_PROJECTION_READY"] = "true"
    with pytest.raises(RuntimeError, match="complete backfill coverage"):
        validate_artifact_home_projection_activation()


def test_postgres_projection_isolates_one_corrupt_title_without_decrypting_body() -> None:
    _reset()
    codec = GovernedPayloadCodec()
    tenant_id, user_id = 7104, "8204"
    artifact_ids = [
        _insert_artifact(
            codec, tenant_id=tenant_id, user_id=user_id, title=f"draft-{index}",
            body=f"private-body-{index}",
            updated_at=datetime.now(UTC) - timedelta(minutes=index),
        )
        for index in range(3)
    ]
    backfill = PostgresArtifactHomeProjectionBackfill(DATABASE_URL)
    assert backfill.process_batch(limit=10) == 3
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE ai_artifact_drafts SET home_title_envelope = 'dwp2.invalid' "
            "WHERE artifact_id = %s",
            (artifact_ids[1],),
        )

    from dwp_agent.artifact_postgres_store import PostgresArtifactStore

    projection = PostgresArtifactStore(DATABASE_URL).home_projection(
        _identity(tenant_id, user_id), limit=3
    )
    assert projection.visible_count == 3
    assert [slot.title if slot else None for slot in projection.slots] == [
        "draft-0", None, "draft-2"
    ]


def _insert_artifact(
    codec: GovernedPayloadCodec,
    *,
    tenant_id: int,
    user_id: str,
    title: str,
    body: str,
    state: str = "DRAFT",
    updated_at: datetime | None = None,
):
    artifact_id = uuid4()
    envelope = codec.encrypt_json(
        {"title": title, "body": body}, tenant_id=tenant_id,
        resource_type="artifact-draft", resource_id=str(artifact_id), field="content",
    )
    now = updated_at or datetime.now(UTC)
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """INSERT INTO ai_artifacts (
                   artifact_id, tenant_id, user_id, artifact_type, artifact_state,
                   retention_until, created_at, updated_at, archived_at)
               VALUES (%s, %s, %s, 'DOCUMENT', %s, %s, %s, %s, %s)""",
            (
                artifact_id, tenant_id, user_id, state, now + timedelta(days=30),
                now, now, now if state == "ARCHIVED" else None,
            ),
        )
        connection.execute(
            """INSERT INTO ai_artifact_drafts (
                   artifact_id, tenant_id, user_id, draft_revision, content_envelope,
                   content_fingerprint, updated_by_user_id, updated_at)
               VALUES (%s, %s, %s, 1, %s, %s, %s, %s)""",
            (artifact_id, tenant_id, user_id, envelope, "d" * 64, user_id, now),
        )
    return artifact_id


def _identity(tenant_id: int, user_id: str) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant_id, user_id=user_id, correlation_id="home-integration",
        auth_session_id="home-integration", roles=frozenset(), permissions=frozenset(),
    )


def _reset() -> None:
    with connect(DATABASE_URL) as connection:
        connection.execute("TRUNCATE ai_artifacts CASCADE")
        connection.execute("TRUNCATE agent_home_identity_assertion_replay")
        connection.execute("TRUNCATE agent_artifact_home_projection_backfill_receipts")
