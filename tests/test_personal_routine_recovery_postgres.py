from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from psycopg import connect
from psycopg.rows import dict_row

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.personal_routine_contracts import (
    CommandRoutineRunRequest,
    RoutineDefinition,
)
from dwp_agent.personal_routine_execution_provider import (
    RoutineExecutionProvider,
    RoutineExecutionProviderConfiguration,
    RoutineExecutionRuntimeControls,
    runtime_controls_digest,
)
from dwp_agent.personal_routine_execution_worker import (
    PostgresPersonalRoutineExecutionWorker,
)
from dwp_agent.personal_routine_postgres_store import PostgresPersonalRoutineStore


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Routine recovery tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


def test_skip_quarantined_records_continues_with_a_sealed_recovery_receipt() -> None:
    tenant_id = int(uuid4().int % 8_000_000) + 30_000_000
    user_id = f"routine-recovery-{uuid4()}"
    routine_id = uuid4()
    run_id = uuid4()
    command_id = uuid4()
    now = datetime.now(UTC)
    definition = RoutineDefinition(
        name="Supplier variance briefing",
        objective="Create an approval-gated briefing from verified records.",
        cadence="WEEKDAYS",
        local_time="09:00",
        time_zone="Asia/Seoul",
        locale="ko-KR",
        sources=["WORK_ITEM"],
    )
    configuration = RoutineExecutionProviderConfiguration(
        enabled=True,
        base_url="https://routine-broker.internal.example",
        service_token="test-service-token",
        allowed_hosts=frozenset({"routine-broker.internal.example"}),
        timeout_seconds=5,
    )
    runtime_controls = RoutineExecutionRuntimeControls(
        engineOverride=None,
        budgetExceptionCommandIds=[],
        additionalTokensPerRun=0,
        additionalMinutesPerRun=0,
    )

    def provider_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["recoveryDirective"]["action"] == (
            "SKIP_QUARANTINED_AND_CONTINUE"
        )
        assert body["recoveryDirective"]["commandId"] == str(command_id)
        assert body["recoveryDirective"]["priorProviderReceiptId"] == (
            "provider-partial-receipt"
        )
        return httpx.Response(
            200,
            json={
                "routineRunId": str(run_id),
                "state": "COMPLETED",
                "providerReceiptId": "provider-completed-receipt",
                "resultSha256": hashlib.sha256(b"completed-after-skip").hexdigest(),
                "evidenceCount": 1177,
                "proposalsCreated": 1,
                "approvalGatedActionsCreated": 1,
                "externalWritesPerformed": 0,
                "tokensUsed": 4096,
                "elapsedMs": 2300,
                "notificationState": "DELIVERED",
                "compensationRequired": False,
                "authorizationDecisionRevision": 22,
                "authorizedSources": ["WORK_ITEM"],
                "routineId": str(routine_id),
                "routineRevision": 1,
                "appliedRuntimeControls": runtime_controls.model_dump(mode="json"),
                "runtimeControlsSha256": runtime_controls_digest(runtime_controls),
            },
        )

    provider = RoutineExecutionProvider(
        configuration, transport=httpx.MockTransport(provider_handler)
    )
    worker = PostgresPersonalRoutineExecutionWorker(DATABASE_URL, provider=provider)
    definition_payload = definition.model_dump(mode="json", by_alias=True)
    definition_envelope = worker.codec.encrypt_json(
        definition_payload,
        tenant_id=tenant_id,
        resource_type="personal-routine",
        resource_id=str(routine_id),
        field="definition",
    )
    definition_fingerprint = worker.fingerprints.value(
        tenant_id=tenant_id,
        purpose="personal-routine-definition",
        payload=definition_payload,
    )
    provider_receipt_envelope = worker.codec.encrypt_json(
        {
            "providerReceiptId": "provider-partial-receipt",
            "authorizationDecisionRevision": 21,
            "authorizedSources": ["WORK_ITEM"],
        },
        tenant_id=tenant_id,
        resource_type="personal-routine-execution",
        resource_id=str(run_id),
        field="provider-receipt",
    )
    with connect(DATABASE_URL) as connection:
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
                routine_id,
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
                   correlation_id, provider_receipt_envelope, result_sha256,
                   safe_error_code, recovery_hint, completed_at)
               VALUES (%s, %s, %s, %s, 1, 'MANUAL', 'PARTIAL', 2,
                       %s, 1, 3, %s, %s, %s,
                       'SOURCE_RECORDS_QUARANTINED', %s, %s)""",
            (
                run_id,
                routine_id,
                tenant_id,
                user_id,
                now,
                f"routine-recovery:{run_id}",
                provider_receipt_envelope,
                hashlib.sha256(b"partial").hexdigest(),
                "Review quarantined records and choose a recovery action.",
                now,
            ),
        )

    identity = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        correlation_id=f"routine-recovery-command:{run_id}",
        auth_session_id=f"routine-recovery-session:{run_id}",
        roles=frozenset({"EMPLOYEE"}),
        permissions=frozenset({"APP.DWAION_ROUTINES:MANAGE"}),
    )
    store = PostgresPersonalRoutineStore(DATABASE_URL)
    request = CommandRoutineRunRequest(
        commandId=command_id,
        expectedRevision=2,
        reasonCode="PARTIAL_SOURCE_QUARANTINED",
        changeReason="Continue with verified records after quarantining failed records.",
        action="SKIP_QUARANTINED_AND_CONTINUE",
    )
    queued = store.command_run(identity, routine_id, run_id, request)
    assert queued.state.value == "QUEUED"
    assert queued.recovery_action.value == "SKIP_QUARANTINED_AND_CONTINUE"
    assert queued.recovery_command_id == command_id

    assert worker.process_once() is True
    completed = store.get_run(identity, routine_id, run_id)
    assert completed.state.value == "COMPLETED"
    assert completed.receipt is not None
    assert completed.receipt.recovery_action is not None
    assert completed.receipt.recovery_command_id == command_id
    assert completed.receipt.provider_receipt_id == "provider-completed-receipt"

    with connect(DATABASE_URL, row_factory=dict_row) as connection:
        evidence = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_personal_routine_commands
                     WHERE command_id = %s AND command_type = 'RUN_COMMAND') AS commands,
                   (SELECT COUNT(*) FROM ai_personal_routine_events
                     WHERE command_id = %s
                       AND event_type = 'RUN_SKIP_QUARANTINED_REQUESTED') AS events,
                   (SELECT COUNT(*) FROM ai_personal_routine_execution_events
                     WHERE routine_run_id = %s AND event_type = 'COMPLETED') AS completions""",
            (command_id, command_id, run_id),
        ).fetchone()
    assert evidence is not None
    assert (evidence["commands"], evidence["events"], evidence["completions"]) == (
        1,
        1,
        1,
    )
