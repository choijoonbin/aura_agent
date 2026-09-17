from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from dwp_agent.personal_routine_advanced_contracts import (
    CreateRoutineAdvancedCommandRequest,
    DecideRoutineAdvancedCommandRequest,
    RoutineAdvancedCommandReceipt,
    RoutineAdvancedCommandKind,
)
from dwp_agent.personal_routine_advanced_provider import (
    HttpRoutineAdvancedProvider,
    RoutineAdvancedProviderContext,
    RoutineAdvancedProviderError,
    RoutineAdvancedProviderOutcome,
    RoutineAdvancedProviderResult,
    provider_result_digest,
    routine_advanced_capability,
)


def _configure_provider(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    prefix = f"DWP_ROUTINE_ADVANCED_{kind}"
    monkeypatch.setenv(f"{prefix}_URL", "https://routine-provider.internal/v1/commands")
    monkeypatch.setenv(f"{prefix}_TOKEN", "routine-provider-test-token-123456")
    monkeypatch.setenv("DWP_ROUTINE_ADVANCED_ALLOWED_HOSTS", "routine-provider.internal")


def _outcome(
    kind: RoutineAdvancedCommandKind,
    routine_id,
    expected_revision: int,
    payload: dict[str, object],
) -> RoutineAdvancedProviderOutcome:
    return RoutineAdvancedProviderOutcome(
        routineId=routine_id,
        expectedRevision=expected_revision,
        kind=kind,
        outcome="APPLIED",
        appliedPayload=payload,
        evidenceRef=f"provider-evidence:{kind.value.lower()}",
    )


def test_http_provider_binds_command_kind_headers_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = RoutineAdvancedCommandKind.WORM_EVIDENCE_DELIVERY
    _configure_provider(monkeypatch, kind.value)
    command_id = uuid4()
    routine_id = uuid4()
    payload = {
        "kind": kind.value,
        "evidenceScope": "FULL_AUDIT",
        "retentionDays": 365,
        "legalHold": True,
    }
    outcome = _outcome(kind, routine_id, 3, payload)
    digest = provider_result_digest(outcome)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/commands"
        assert request.headers["authorization"] == "Bearer routine-provider-test-token-123456"
        assert request.headers["x-dwp-idempotency-key"] == str(command_id)
        assert request.headers["x-dwp-command-id"] == str(command_id)
        assert request.headers["x-dwp-tenant-id"] == "71"
        assert body == {
            "commandId": str(command_id),
            "routineId": str(routine_id),
            "kind": kind.value,
            "expectedRevision": 3,
            "payload": {
                "kind": kind.value,
                "evidenceScope": "FULL_AUDIT",
                "retentionDays": 365,
                "legalHold": True,
            },
        }
        return httpx.Response(
            200,
            json={
                "data": {
                    "commandId": str(command_id),
                    "kind": kind.value,
                    "state": "SUCCEEDED",
                    "providerReceiptId": "provider-receipt-71",
                    "resultSha256": digest,
                    "appliedRevision": 4,
                    "result": outcome.model_dump(mode="json", by_alias=True),
                }
            },
        )

    result = HttpRoutineAdvancedProvider(
        kind, transport=httpx.MockTransport(handler)
    ).execute(
        RoutineAdvancedProviderContext(
            command_id=command_id,
            routine_id=routine_id,
            tenant_id=71,
            user_id="member-1",
            correlation_id="routine-advanced-test",
            kind=kind,
            expected_revision=3,
            payload=payload,
        )
    )

    assert result.provider_receipt_id == "provider-receipt-71"
    assert result.result_sha256 == digest
    assert result.applied_revision == 4
    assert routine_advanced_capability(kind).available is True


def test_http_provider_rejects_receipt_bound_to_another_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = RoutineAdvancedCommandKind.OPERATOR_ESCALATION
    _configure_provider(monkeypatch, kind.value)
    command_id = uuid4()
    routine_id = uuid4()
    payload = {
        "kind": kind.value,
        "severity": "P2",
        "summary": "Escalate the governed routine to an operator.",
    }
    outcome = _outcome(kind, routine_id, 1, payload)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "commandId": str(uuid4()),
                "kind": kind.value,
                "state": "SUCCEEDED",
                "providerReceiptId": "wrong-command-receipt",
                "resultSha256": provider_result_digest(outcome),
                "result": outcome.model_dump(mode="json", by_alias=True),
            },
        )

    with pytest.raises(
        RoutineAdvancedProviderError,
        match="ROUTINE_ADVANCED_PROVIDER_BINDING_INVALID",
    ):
        HttpRoutineAdvancedProvider(
            kind, transport=httpx.MockTransport(handler)
        ).execute(
            RoutineAdvancedProviderContext(
                command_id=command_id,
                routine_id=routine_id,
                tenant_id=71,
                user_id="member-1",
                correlation_id="routine-advanced-test",
                kind=kind,
                expected_revision=1,
                payload=payload,
            )
        )


def test_http_provider_rejects_whitespace_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = RoutineAdvancedCommandKind.OAUTH_REAUTHORIZATION
    _configure_provider(monkeypatch, kind.value)
    command_id = uuid4()
    routine_id = uuid4()
    payload = {
        "kind": kind.value,
        "source": "WORK_ITEM",
        "connectionReference": "connection:primary",
    }
    outcome = _outcome(kind, routine_id, 1, payload)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "commandId": str(command_id),
                "kind": kind.value,
                "state": "SUCCEEDED",
                "providerReceiptId": "   ",
                "resultSha256": provider_result_digest(outcome),
                "result": outcome.model_dump(mode="json", by_alias=True),
            },
        )

    with pytest.raises(
        RoutineAdvancedProviderError,
        match="ROUTINE_ADVANCED_PROVIDER_RECEIPT_INVALID",
    ):
        HttpRoutineAdvancedProvider(
            kind, transport=httpx.MockTransport(handler)
        ).execute(
            RoutineAdvancedProviderContext(
                command_id=command_id,
                routine_id=routine_id,
                tenant_id=71,
                user_id="member-1",
                correlation_id="routine-advanced-blank-receipt",
                kind=kind,
                expected_revision=1,
                payload=payload,
            )
        )


def test_http_provider_rejects_digest_and_context_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = RoutineAdvancedCommandKind.PROVIDER_ROLLBACK
    _configure_provider(monkeypatch, kind.value)
    command_id = uuid4()
    routine_id = uuid4()
    payload = {
        "kind": kind.value,
        "routineRunId": str(uuid4()),
        "providerReceiptId": "provider-receipt-before-rollback",
    }
    wrong = _outcome(kind, uuid4(), 2, payload)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "commandId": str(command_id),
                "kind": kind.value,
                "state": "SUCCEEDED",
                "providerReceiptId": "rollback-receipt",
                "resultSha256": provider_result_digest(wrong),
                "result": wrong.model_dump(mode="json", by_alias=True),
            },
        )

    context = RoutineAdvancedProviderContext(
        command_id=command_id,
        routine_id=routine_id,
        tenant_id=71,
        user_id="member-1",
        correlation_id="routine-advanced-binding-test",
        kind=kind,
        expected_revision=2,
        payload=payload,
    )
    with pytest.raises(
        RoutineAdvancedProviderError,
        match="ROUTINE_ADVANCED_PROVIDER_BINDING_INVALID",
    ):
        HttpRoutineAdvancedProvider(
            kind, transport=httpx.MockTransport(handler)
        ).execute(context)

    valid = _outcome(kind, routine_id, 2, payload)

    def bad_digest_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "commandId": str(command_id),
                "kind": kind.value,
                "state": "SUCCEEDED",
                "providerReceiptId": "rollback-receipt",
                "resultSha256": "0" * 64,
                "result": valid.model_dump(mode="json", by_alias=True),
            },
        )

    with pytest.raises(
        RoutineAdvancedProviderError,
        match="ROUTINE_ADVANCED_PROVIDER_RECEIPT_INVALID",
    ):
        HttpRoutineAdvancedProvider(
            kind, transport=httpx.MockTransport(bad_digest_handler)
        ).execute(context)


def test_runtime_effect_commands_require_their_attested_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for kind in (
        RoutineAdvancedCommandKind.AGENT_ENGINE_SWITCH,
        RoutineAdvancedCommandKind.TEMPORARY_BUDGET_INCREASE,
    ):
        _configure_provider(monkeypatch, kind.value)
        capability = routine_advanced_capability(kind)
        assert capability.available is True
        assert capability.configured is True
        assert capability.reason_code is None
        assert HttpRoutineAdvancedProvider(kind).configured is True


def test_advanced_contracts_reject_unbounded_or_ambiguous_commands() -> None:
    common = {
        "commandId": str(uuid4()),
        "expectedRevision": 1,
        "reasonCode": "USER_ROUTINE_ADVANCED",
        "changeReason": "Apply the reviewed advanced routine control.",
    }
    with pytest.raises(ValidationError):
        CreateRoutineAdvancedCommandRequest.model_validate(
            {
                **common,
                "payload": {
                    "kind": "TEMPORARY_BUDGET_INCREASE",
                    "additionalRuns": 0,
                    "additionalTokensPerRun": 0,
                    "additionalMinutesPerRun": 0,
                    "expiresAt": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                },
            }
        )
    for invalid_engine_payload in (
        {
            "kind": "AGENT_ENGINE_SWITCH",
            "action": "APPLY",
            "agentId": "routine-agent",
            "engineId": "engine-v2",
        },
        {
            "kind": "AGENT_ENGINE_SWITCH",
            "action": "ROLLBACK",
            "agentId": "routine-agent",
        },
        {
            "kind": "AGENT_ENGINE_SWITCH",
            "action": "APPLY",
            "agentId": "routine-agent",
            "engineId": "engine-v2",
            "expiresAt": (datetime.now(UTC) + timedelta(days=31)).isoformat(),
        },
    ):
        with pytest.raises(ValidationError):
            CreateRoutineAdvancedCommandRequest.model_validate(
                {**common, "payload": invalid_engine_payload}
            )
    with pytest.raises(ValidationError, match="summary is too short"):
        CreateRoutineAdvancedCommandRequest.model_validate(
            {
                **common,
                "payload": {
                    "kind": "OPERATOR_ESCALATION",
                    "severity": "P2",
                    "summary": "          ",
                },
            }
        )
    with pytest.raises(ValidationError):
        DecideRoutineAdvancedCommandRequest.model_validate(
            {
                **common,
                "decision": "APPROVE",
                "evidenceRefs": ["ticket:71", "ticket:71"],
            }
        )
    with pytest.raises(ValidationError):
        DecideRoutineAdvancedCommandRequest.model_validate(
            {**common, "decision": "REJECT", "evidenceRefs": []}
        )
    with pytest.raises(ValidationError):
        DecideRoutineAdvancedCommandRequest.model_validate(
            {**common, "decision": "APPROVE", "evidenceRefs": ["x" * 241]}
        )
    with pytest.raises(ValidationError, match="must not be blank"):
        DecideRoutineAdvancedCommandRequest.model_validate(
            {**common, "decision": "APPROVE", "evidenceRefs": ["   "]}
        )
    normalized = DecideRoutineAdvancedCommandRequest.model_validate(
        {**common, "decision": "APPROVE", "evidenceRefs": ["  ticket:71  "]}
    )
    assert normalized.evidence_refs == ["ticket:71"]
    with pytest.raises(ValidationError, match="must not be blank"):
        CreateRoutineAdvancedCommandRequest.model_validate(
            {
                **common,
                "payload": {
                    "kind": "PROVIDER_ROLLBACK",
                    "routineRunId": str(uuid4()),
                    "providerReceiptId": "   ",
                },
            }
        )
    with pytest.raises(ValidationError, match="must not be blank"):
        RoutineAdvancedCommandReceipt.model_validate(
            {
                "receiptId": str(uuid4()),
                "commandId": str(uuid4()),
                "routineId": str(uuid4()),
                "kind": "PROVIDER_ROLLBACK",
                "state": "SUCCEEDED",
                "providerReceiptId": "   ",
                "resultSha256": hashlib.sha256(b"receipt").hexdigest(),
                "completedAt": datetime.now(UTC).isoformat(),
            }
        )


def test_provider_result_requires_complete_partial_recovery_evidence() -> None:
    kind = RoutineAdvancedCommandKind.PROVIDER_ROLLBACK
    payload = {
        "kind": kind.value,
        "routineRunId": str(uuid4()),
        "providerReceiptId": "provider-receipt-before-rollback",
    }
    outcome = RoutineAdvancedProviderOutcome(
        routineId=uuid4(),
        expectedRevision=1,
        kind=kind,
        outcome="PARTIALLY_APPLIED",
        appliedPayload=payload,
        evidenceRef="provider-evidence:partial-rollback",
    )
    with pytest.raises(ValidationError):
        RoutineAdvancedProviderResult(
            commandId=uuid4(),
            kind=kind,
            state="PARTIAL",
            providerReceiptId="partial-receipt",
            resultSha256=provider_result_digest(outcome),
            result=outcome,
            problemCode="ROLLBACK_PARTIAL",
        )
