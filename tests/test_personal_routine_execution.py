from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from dwp_agent.personal_routine_contracts import (
    RoutineDefinition,
    RoutineExecutionReceipt,
    RoutineExecutionRun,
)
from dwp_agent.personal_routine_execution_provider import (
    RoutineExecutionProvider,
    RoutineExecutionProviderConfiguration,
    RoutineExecutionProviderUnavailable,
)


ROOT = Path(__file__).resolve().parents[1]


def _configuration() -> RoutineExecutionProviderConfiguration:
    return RoutineExecutionProviderConfiguration(
        enabled=True,
        base_url="https://routine-broker.internal.example",
        service_token="test-service-token",
        allowed_hosts=frozenset({"routine-broker.internal.example"}),
        timeout_seconds=5,
    )


def _definition() -> RoutineDefinition:
    return RoutineDefinition(
        name="Morning priorities",
        objective="Prepare approval-gated work proposals",
        cadence="WEEKDAYS",
        local_time="09:00",
        time_zone="Asia/Seoul",
        locale="ko-KR",
        sources=["WORK_ITEM"],
    )


def test_routine_provider_executes_only_approval_gated_actions() -> None:
    run_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/internal/v1/routine-executions"
        assert body["routineRunId"] == str(run_id)
        assert body["externalWritesAllowed"] is False
        assert body["approvalGatedActionsOnly"] is True
        assert body["requireCurrentAuthorization"] is True
        assert request.headers["authorization"] == "Bearer test-service-token"
        return httpx.Response(
            200,
            json={
                "routineRunId": str(run_id),
                "state": "COMPLETED",
                "providerReceiptId": "provider-receipt-1",
                "resultSha256": hashlib.sha256(b"result").hexdigest(),
                "evidenceCount": 3,
                "proposalsCreated": 1,
                "approvalGatedActionsCreated": 2,
                "externalWritesPerformed": 0,
                "tokensUsed": 400,
                "elapsedMs": 1200,
                "notificationState": "DELIVERED",
                "compensationRequired": False,
                "authorizationDecisionRevision": 14,
                "authorizedSources": ["WORK_ITEM"],
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    result = provider.execute(
        routine_run_id=run_id,
        routine_id=uuid4(),
        routine_revision=2,
        tenant_id=7,
        user_id="member-1",
        correlation_id="correlation-1",
        definition=_definition(),
    )

    assert result.state == "COMPLETED"
    assert result.externalWritesPerformed == 0
    assert result.approvalGatedActionsCreated == 2


def test_routine_provider_rejects_external_write_claims() -> None:
    run_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "routineRunId": str(uuid4()),
                "state": "COMPLETED",
                "providerReceiptId": "provider-receipt-1",
                "resultSha256": hashlib.sha256(b"result").hexdigest(),
                "evidenceCount": 1,
                "proposalsCreated": 0,
                "approvalGatedActionsCreated": 0,
                "externalWritesPerformed": 1,
                "tokensUsed": 50,
                "elapsedMs": 50,
                "notificationState": "NOT_REQUIRED",
                "authorizationDecisionRevision": 14,
                "authorizedSources": ["WORK_ITEM"],
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RoutineExecutionProviderUnavailable):
        provider.execute(
            routine_run_id=run_id,
            routine_id=uuid4(),
            routine_revision=1,
            tenant_id=7,
            user_id="member-1",
            correlation_id="correlation-1",
            definition=_definition(),
        )


def test_routine_provider_rejects_run_or_source_authorization_mismatch() -> None:
    run_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "routineRunId": str(uuid4()),
                "state": "COMPLETED",
                "providerReceiptId": "provider-receipt-2",
                "resultSha256": hashlib.sha256(b"result").hexdigest(),
                "evidenceCount": 1,
                "proposalsCreated": 0,
                "approvalGatedActionsCreated": 0,
                "externalWritesPerformed": 0,
                "tokensUsed": 50,
                "elapsedMs": 50,
                "notificationState": "NOT_REQUIRED",
                "authorizationDecisionRevision": 15,
                "authorizedSources": ["MAIL"],
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RoutineExecutionProviderUnavailable):
        provider.execute(
            routine_run_id=run_id,
            routine_id=uuid4(),
            routine_revision=1,
            tenant_id=7,
            user_id="member-1",
            correlation_id="correlation-2",
            definition=_definition(),
        )


def test_routine_provider_configuration_is_fail_closed() -> None:
    configuration = RoutineExecutionProviderConfiguration(
        enabled=True,
        base_url="http://unapproved.example",
        service_token="token",
        allowed_hosts=frozenset({"unapproved.example"}),
        timeout_seconds=5,
    )

    assert configuration.configured is False
    with pytest.raises(RoutineExecutionProviderUnavailable):
        configuration.validate()


def test_success_receipt_is_only_valid_for_completed_terminal_runs() -> None:
    now = datetime.now(UTC)
    run_id = uuid4()
    routine_id = uuid4()
    receipt = RoutineExecutionReceipt(
        receipt_id=uuid4(),
        routine_run_id=run_id,
        routine_id=routine_id,
        routine_revision=3,
        terminal_state="COMPLETED",
        provider_receipt_id="provider-receipt-1",
        result_sha256=hashlib.sha256(b"result").hexdigest(),
        evidence_count=2,
        proposals_created=1,
        approval_gated_actions_created=1,
        external_writes_performed=0,
        notification_state="DELIVERED",
        authorization_decision_revision=14,
        authorized_sources=["WORK_ITEM"],
        completed_at=now,
    )
    run = RoutineExecutionRun(
        routine_run_id=run_id,
        routine_id=routine_id,
        routine_revision=3,
        trigger="SCHEDULED",
        state="COMPLETED",
        version=4,
        attempt_count=1,
        maximum_attempts=3,
        scheduled_for=now,
        completed_at=now,
        receipt=receipt,
        created_at=now,
        updated_at=now,
    )
    assert run.receipt == receipt

    with pytest.raises(ValidationError):
        RoutineExecutionRun(
            routine_run_id=run_id,
            routine_id=routine_id,
            routine_revision=3,
            trigger="SCHEDULED",
            state="RUNNING",
            version=3,
            attempt_count=1,
            maximum_attempts=3,
            scheduled_for=now,
            receipt=receipt,
            created_at=now,
            updated_at=now,
        )


def test_v39_forward_migration_enables_fenced_execution_without_rewriting_v24() -> None:
    migration = (
        ROOT
        / "src/dwp_agent/migrations/V39__execute_governed_personal_routines.sql"
    ).read_text(encoding="utf-8")
    original = (
        ROOT / "src/dwp_agent/migrations/V24__create_personal_ai_routines.sql"
    ).read_text(encoding="utf-8")

    assert "DROP CONSTRAINT ck_ai_personal_routine_mode" in migration
    assert "execution_mode IN ('DRY_RUN_ONLY', 'SCHEDULED')" in migration
    assert "ai_personal_routine_executions" in migration
    assert "lease_generation" in migration
    assert "external_writes_performed = 0" in migration
    assert "run_state IN ('COMPLETED', 'COMPENSATED')" in migration
    assert "receipt_fingerprint" in migration
    assert "reject_ai_audit_event_mutation" in migration
    assert "V24 is intentionally DRY_RUN_ONLY" in original
