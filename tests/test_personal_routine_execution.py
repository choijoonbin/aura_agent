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
from dwp_agent.personal_routine_capabilities import routine_runtime_capabilities
from dwp_agent.personal_routine_execution_provider import (
    RoutineExecutionProvider,
    RoutineExecutionProviderConfiguration,
    RoutineExecutionProviderUnavailable,
    RoutineExecutionRuntimeControls,
    RoutineProviderCompensationResult,
    RoutineProviderResult,
    runtime_controls_digest,
)


ROOT = Path(__file__).resolve().parents[1]


def test_routine_provider_only_capabilities_publish_recovery_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DWP_ROUTINE_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("DWP_ROUTINE_EXECUTION_BROKER_BASE_URL", raising=False)
    monkeypatch.delenv("DWP_ROUTINE_EXECUTION_BROKER_SERVICE_TOKEN", raising=False)

    payload = routine_runtime_capabilities().model_dump(mode="json", by_alias=True)

    assert payload["oauthReauthorization"]["available"] is False
    assert payload["oauthReauthorization"]["configured"] is False
    assert payload["oauthReauthorization"]["reasonCode"] == (
        "ROUTINE_OAUTH_REAUTHORIZATION_NOT_CONFIGURED"
    )
    assert payload["temporaryBudgetIncrease"]["recoveryHint"]
    assert payload["operatorEscalation"]["recoveryHint"]
    assert payload["providerRollback"]["recoveryHint"]


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


def _runtime_attestation(
    routine_id,
    revision: int = 1,
    controls: dict[str, object] | None = None,
) -> dict[str, object]:
    applied = RoutineExecutionRuntimeControls.model_validate(
        controls
        or {
            "engineOverride": None,
            "budgetExceptionCommandIds": [],
            "additionalTokensPerRun": 0,
            "additionalMinutesPerRun": 0,
        }
    )
    return {
        "routineId": str(routine_id),
        "routineRevision": revision,
        "appliedRuntimeControls": applied.model_dump(mode="json"),
        "runtimeControlsSha256": runtime_controls_digest(applied),
    }


def test_routine_provider_executes_only_approval_gated_actions() -> None:
    run_id = uuid4()
    routine_id = uuid4()
    runtime_controls = {
        "engineOverride": {
            "agentId": "routine-agent",
            "engineId": "engine-v2",
            "stateVersion": 2,
            "sourceCommandId": str(uuid4()),
            "expiresAt": "2026-09-20T00:00:00+00:00",
        },
        "budgetExceptionCommandIds": [str(uuid4())],
        "additionalTokensPerRun": 10_000,
        "additionalMinutesPerRun": 5,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/internal/v1/routine-executions"
        assert body["routineRunId"] == str(run_id)
        assert body["externalWritesAllowed"] is False
        assert body["approvalGatedActionsOnly"] is True
        assert body["requireCurrentAuthorization"] is True
        assert body["runtimeControls"] == RoutineExecutionRuntimeControls.model_validate(
            runtime_controls
        ).model_dump(mode="json")
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
                **_runtime_attestation(routine_id, 2, runtime_controls),
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    result = provider.execute(
        routine_run_id=run_id,
        routine_id=routine_id,
        routine_revision=2,
        tenant_id=7,
        user_id="member-1",
        correlation_id="correlation-1",
        definition=_definition(),
        runtime_controls=runtime_controls,
    )

    assert result.state == "COMPLETED"
    assert result.externalWritesPerformed == 0
    assert result.approvalGatedActionsCreated == 2


def test_routine_provider_models_reject_whitespace_terminal_receipts() -> None:
    routine_id = uuid4()
    result = {
        "routineRunId": str(uuid4()),
        "providerReceiptId": "   ",
        "resultSha256": hashlib.sha256(b"blank").hexdigest(),
    }
    with pytest.raises(ValidationError, match="must not be blank"):
        RoutineProviderResult.model_validate({
            **result,
            "state": "COMPLETED",
            "evidenceCount": 0,
            "proposalsCreated": 0,
            "approvalGatedActionsCreated": 0,
            "externalWritesPerformed": 0,
            "tokensUsed": 0,
            "elapsedMs": 0,
            "notificationState": "NOT_REQUIRED",
            "authorizationDecisionRevision": 1,
            "authorizedSources": ["WORK_ITEM"],
            **_runtime_attestation(routine_id),
        })
    with pytest.raises(ValidationError, match="must not be blank"):
        RoutineProviderCompensationResult.model_validate({
            **result,
            "revokedPendingHandoffs": 0,
            "externalWritesReversed": 0,
        })


def test_routine_provider_transport_rejects_whitespace_execution_and_compensation_receipts() -> None:
    run_id = uuid4()
    routine_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/compensate"):
            return httpx.Response(
                200,
                json={
                    "routineRunId": str(run_id),
                    "providerReceiptId": "   ",
                    "resultSha256": hashlib.sha256(b"compensation").hexdigest(),
                    "revokedPendingHandoffs": 0,
                    "externalWritesReversed": 0,
                },
            )
        return httpx.Response(
            200,
            json={
                "routineRunId": str(run_id),
                "state": "COMPLETED",
                "providerReceiptId": "   ",
                "resultSha256": hashlib.sha256(b"execution").hexdigest(),
                "evidenceCount": 0,
                "proposalsCreated": 0,
                "approvalGatedActionsCreated": 0,
                "externalWritesPerformed": 0,
                "tokensUsed": 0,
                "elapsedMs": 0,
                "notificationState": "NOT_REQUIRED",
                "authorizationDecisionRevision": 1,
                "authorizedSources": ["WORK_ITEM"],
                **_runtime_attestation(routine_id),
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(
        RoutineExecutionProviderUnavailable,
        match="ROUTINE_EXECUTION_PROVIDER_UNAVAILABLE",
    ):
        provider.execute(
            routine_run_id=run_id,
            routine_id=routine_id,
            routine_revision=1,
            tenant_id=7,
            user_id="member-1",
            correlation_id="blank-execution-receipt",
            definition=_definition(),
        )
    with pytest.raises(
        RoutineExecutionProviderUnavailable,
        match="ROUTINE_EXECUTION_PROVIDER_UNAVAILABLE",
    ):
        provider.compensate(
            routine_run_id=run_id,
            provider_receipt_id="prior-provider-receipt",
            tenant_id=7,
            user_id="member-1",
            correlation_id="blank-compensation-receipt",
        )


def test_routine_provider_binds_skip_quarantine_recovery_to_prior_receipt() -> None:
    run_id = uuid4()
    routine_id = uuid4()
    command_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["recoveryDirective"] == {
            "action": "SKIP_QUARANTINED_AND_CONTINUE",
            "commandId": str(command_id),
            "reasonCode": "PARTIAL_SOURCE_QUARANTINED",
            "changeReason": "Continue with verified records after quarantine.",
            "priorProviderReceiptId": "provider-partial-receipt-1",
        }
        return httpx.Response(
            200,
            json={
                "routineRunId": str(run_id),
                "state": "COMPLETED",
                "providerReceiptId": "provider-recovery-receipt-2",
                "resultSha256": hashlib.sha256(b"recovered").hexdigest(),
                "evidenceCount": 7,
                "proposalsCreated": 1,
                "approvalGatedActionsCreated": 1,
                "externalWritesPerformed": 0,
                "tokensUsed": 800,
                "elapsedMs": 1800,
                "notificationState": "DELIVERED",
                "authorizationDecisionRevision": 18,
                "authorizedSources": ["WORK_ITEM"],
                **_runtime_attestation(routine_id, 3),
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    result = provider.execute(
        routine_run_id=run_id,
        routine_id=routine_id,
        routine_revision=3,
        tenant_id=7,
        user_id="member-1",
        correlation_id="correlation-recovery-1",
        definition=_definition(),
        recovery_directive={
            "action": "SKIP_QUARANTINED_AND_CONTINUE",
            "commandId": str(command_id),
            "reasonCode": "PARTIAL_SOURCE_QUARANTINED",
            "changeReason": "Continue with verified records after quarantine.",
            "priorProviderReceiptId": "provider-partial-receipt-1",
        },
    )

    assert result.state == "COMPLETED"
    assert result.providerReceiptId == "provider-recovery-receipt-2"


def test_routine_provider_rejects_external_write_claims() -> None:
    run_id = uuid4()
    routine_id = uuid4()

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
                **_runtime_attestation(routine_id),
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RoutineExecutionProviderUnavailable):
        provider.execute(
            routine_run_id=run_id,
            routine_id=routine_id,
            routine_revision=1,
            tenant_id=7,
            user_id="member-1",
            correlation_id="correlation-1",
            definition=_definition(),
        )


def test_routine_provider_rejects_run_or_source_authorization_mismatch() -> None:
    run_id = uuid4()
    routine_id = uuid4()

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
                **_runtime_attestation(routine_id),
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RoutineExecutionProviderUnavailable):
        provider.execute(
            routine_run_id=run_id,
            routine_id=routine_id,
            routine_revision=1,
            tenant_id=7,
            user_id="member-1",
            correlation_id="correlation-2",
            definition=_definition(),
        )


def test_routine_provider_rejects_unapplied_runtime_controls() -> None:
    run_id = uuid4()
    routine_id = uuid4()
    requested = {
        "engineOverride": None,
        "budgetExceptionCommandIds": [str(uuid4())],
        "additionalTokensPerRun": 10_000,
        "additionalMinutesPerRun": 5,
    }
    ignored = RoutineExecutionRuntimeControls(
        engineOverride=None,
        budgetExceptionCommandIds=[],
        additionalTokensPerRun=0,
        additionalMinutesPerRun=0,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "routineRunId": str(run_id),
                "routineId": str(routine_id),
                "routineRevision": 4,
                "state": "COMPLETED",
                "providerReceiptId": "ignored-runtime-controls",
                "resultSha256": hashlib.sha256(b"ignored").hexdigest(),
                "evidenceCount": 0,
                "proposalsCreated": 0,
                "approvalGatedActionsCreated": 0,
                "externalWritesPerformed": 0,
                "tokensUsed": 0,
                "elapsedMs": 0,
                "notificationState": "NOT_REQUIRED",
                "authorizationDecisionRevision": 1,
                "authorizedSources": ["WORK_ITEM"],
                "appliedRuntimeControls": ignored.model_dump(mode="json"),
                "runtimeControlsSha256": runtime_controls_digest(ignored),
            },
        )

    provider = RoutineExecutionProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(
        RoutineExecutionProviderUnavailable,
        match="ROUTINE_EXECUTION_RESPONSE_MISMATCH",
    ):
        provider.execute(
            routine_run_id=run_id,
            routine_id=routine_id,
            routine_revision=4,
            tenant_id=7,
            user_id="member-1",
            correlation_id="runtime-binding-mismatch",
            definition=_definition(),
            runtime_controls=requested,
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
