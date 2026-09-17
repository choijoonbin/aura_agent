from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.rows import dict_row

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.personal_routine_contracts import RoutineDefinition
from dwp_agent.personal_routine_evidence_store import PersonalRoutineEvidenceStore
from dwp_agent.personal_routine_execution_worker import (
    PostgresPersonalRoutineExecutionWorker,
    RoutineExecutionLease,
)


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Routine quarantine tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


def test_exhausted_retry_quarantines_only_the_exact_revision_and_seals_evidence() -> None:
    worker = PostgresPersonalRoutineExecutionWorker(DATABASE_URL)
    tenant_id = int(uuid4().int % 8_000_000) + 1_000_000
    user_id = f"routine-owner-{uuid4()}"
    routine_id = uuid4()
    unrelated_routine_id = uuid4()
    run_id = uuid4()
    lease_token = uuid4()
    now = datetime.now(UTC)
    definition = RoutineDefinition(
        name="Daily governed priorities",
        objective="Create approval-gated proposals from authorized work items.",
        cadence="WEEKDAYS",
        local_time="09:00",
        time_zone="Asia/Seoul",
        locale="ko-KR",
        sources=["WORK_ITEM"],
    )

    with connect(DATABASE_URL) as connection:
        for candidate in (routine_id, unrelated_routine_id):
            definition_envelope = worker.codec.encrypt_json(
                definition.model_dump(mode="json", by_alias=True),
                tenant_id=tenant_id,
                resource_type="personal-routine",
                resource_id=str(candidate),
                field="definition",
            )
            definition_fingerprint = worker.fingerprints.value(
                tenant_id=tenant_id,
                purpose="personal-routine-definition",
                payload=definition.model_dump(mode="json", by_alias=True),
            )
            connection.execute(
                """INSERT INTO ai_personal_routines (
                       routine_id, tenant_id, user_id, lifecycle_state,
                       consent_state, execution_mode, revision,
                       definition_envelope, definition_fingerprint, next_run_at,
                       retention_until, source_access_consent_state,
                       analysis_consent_state, proposal_delivery_consent_state)
                   VALUES (%s, %s, %s, 'ACTIVE', 'ENABLED', 'SCHEDULED', 1,
                           %s, %s, %s, %s, 'ENABLED', 'ENABLED', 'ENABLED')""",
                (
                    candidate,
                    tenant_id,
                    user_id,
                    definition_envelope,
                    definition_fingerprint,
                    now + timedelta(hours=1),
                    now + timedelta(days=365),
                ),
            )
        connection.execute(
            """INSERT INTO ai_personal_routine_executions (
                   routine_run_id, routine_id, tenant_id, user_id,
                   routine_revision, trigger_type, run_state, version,
                   scheduled_for, attempt_count, maximum_attempts,
                   lease_generation, lease_token, lease_expires_at,
                   correlation_id, started_at)
               VALUES (%s, %s, %s, %s, 1, 'SCHEDULED', 'RUNNING', 3,
                       %s, 3, 3, 1, %s, %s, %s, %s)""",
            (
                run_id,
                routine_id,
                tenant_id,
                user_id,
                now,
                lease_token,
                now + timedelta(minutes=5),
                f"routine-quarantine:{run_id}",
                now,
            ),
        )

    lease = RoutineExecutionLease(
        routine_run_id=run_id,
        routine_id=routine_id,
        tenant_id=tenant_id,
        user_id=user_id,
        routine_revision=1,
        generation=1,
        lease_token=lease_token,
        attempt_count=3,
        maximum_attempts=3,
        correlation_id=f"routine-quarantine:{run_id}",
        compensation_requested=False,
    )

    worker._retry_or_fail(lease, "ROUTINE_PROVIDER_UNAVAILABLE")
    worker._retry_or_fail(lease, "ROUTINE_PROVIDER_UNAVAILABLE")

    with connect(DATABASE_URL, row_factory=dict_row) as connection:
        run = connection.execute(
            """SELECT run_state, completed_at, safe_error_code
                 FROM ai_personal_routine_executions
                WHERE routine_run_id = %s""",
            (run_id,),
        ).fetchone()
        quarantined = connection.execute(
            """SELECT lifecycle_state, execution_mode, next_run_at, revision
                 FROM ai_personal_routines WHERE routine_id = %s""",
            (routine_id,),
        ).fetchone()
        unrelated = connection.execute(
            """SELECT lifecycle_state, execution_mode, revision
                 FROM ai_personal_routines WHERE routine_id = %s""",
            (unrelated_routine_id,),
        ).fetchone()
        evidence = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_personal_routine_commands
                     WHERE routine_id = %s AND command_type = 'AUTO_QUARANTINE') AS commands,
                   (SELECT COUNT(*) FROM ai_personal_routine_events
                     WHERE routine_id = %s AND event_type = 'AUTO_QUARANTINED') AS events""",
            (routine_id, routine_id),
        ).fetchone()

    assert run is not None
    assert (run["run_state"], run["safe_error_code"]) == (
        "FAILED",
        "ROUTINE_PROVIDER_UNAVAILABLE",
    )
    assert run["completed_at"] is not None
    assert quarantined is not None
    assert (
        quarantined["lifecycle_state"],
        quarantined["execution_mode"],
        quarantined["next_run_at"],
        quarantined["revision"],
    ) == ("PAUSED", "DRY_RUN_ONLY", None, 2)
    assert unrelated is not None
    assert (
        unrelated["lifecycle_state"],
        unrelated["execution_mode"],
        unrelated["revision"],
    ) == ("ACTIVE", "SCHEDULED", 1)
    assert evidence is not None and (evidence["commands"], evidence["events"]) == (1, 1)

    identity = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        correlation_id=f"routine-evidence:{routine_id}",
        auth_session_id=f"routine-session:{routine_id}",
        roles=frozenset({"EMPLOYEE"}),
        permissions=frozenset({"APP.DWAION_ROUTINES:VIEW"}),
    )
    versions = PersonalRoutineEvidenceStore(DATABASE_URL).versions(identity, routine_id)
    assert versions[0].command_type == "AUTO_QUARANTINE"
    assert versions[0].revision == 2
    assert versions[0].snapshot.lifecycle_state.value == "PAUSED"
    assert versions[0].snapshot.capabilities.automatic_quarantine.available is True


def test_stale_failed_revision_cannot_quarantine_a_newer_routine_revision() -> None:
    worker = PostgresPersonalRoutineExecutionWorker(DATABASE_URL)
    tenant_id = int(uuid4().int % 8_000_000) + 10_000_000
    routine_id = uuid4()
    user_id = f"routine-owner-{uuid4()}"
    definition = RoutineDefinition(
        name="Current routine",
        objective="Keep the newer revision active.",
        cadence="DAILY",
        local_time="08:30",
        time_zone="UTC",
        locale="en-US",
        sources=["WORK_ITEM"],
    )
    envelope = worker.codec.encrypt_json(
        definition.model_dump(mode="json", by_alias=True),
        tenant_id=tenant_id,
        resource_type="personal-routine",
        resource_id=str(routine_id),
        field="definition",
    )
    fingerprint = worker.fingerprints.value(
        tenant_id=tenant_id,
        purpose="personal-routine-definition",
        payload=definition.model_dump(mode="json", by_alias=True),
    )
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """INSERT INTO ai_personal_routines (
                   routine_id, tenant_id, user_id, lifecycle_state,
                   consent_state, execution_mode, revision,
                   definition_envelope, definition_fingerprint, next_run_at,
                   retention_until, source_access_consent_state,
                   analysis_consent_state, proposal_delivery_consent_state)
               VALUES (%s, %s, %s, 'ACTIVE', 'ENABLED', 'SCHEDULED', 2,
                       %s, %s, CURRENT_TIMESTAMP + INTERVAL '1 hour',
                       CURRENT_TIMESTAMP + INTERVAL '365 days',
                       'ENABLED', 'ENABLED', 'ENABLED')""",
            (routine_id, tenant_id, user_id, envelope, fingerprint),
        )
        worker._auto_quarantine(
            connection,
            routine_run_id=uuid4(),
            routine_id=routine_id,
            tenant_id=tenant_id,
            user_id=user_id,
            routine_revision=1,
            correlation_id="stale-run",
            safe_error_code="ROUTINE_PROVIDER_UNAVAILABLE",
        )
    with connect(DATABASE_URL) as connection:
        state = connection.execute(
            """SELECT lifecycle_state, execution_mode, revision
                 FROM ai_personal_routines WHERE routine_id = %s""",
            (routine_id,),
        ).fetchone()
        evidence_count = connection.execute(
            """SELECT COUNT(*) FROM ai_personal_routine_events
                WHERE routine_id = %s AND event_type = 'AUTO_QUARANTINED'""",
            (routine_id,),
        ).fetchone()[0]
    assert state == ("ACTIVE", "SCHEDULED", 2)
    assert evidence_count == 0
