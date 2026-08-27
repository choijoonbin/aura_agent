from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.operational_gate_contracts import (
    BootstrapOperationalGatesRequest,
    ConfigureOperationalGateRequest,
    CreateOperationalGateEvidenceRequest,
    DecideOperationalGateRequest,
    GateActorRole,
    GateApprovalEligibilityReason,
    GateDecision,
    GateEnvironment,
    GateEvidenceType,
    GateStatus,
    GateValidationOutcome,
    OperationalGateKey,
    ValidateOperationalGateRequest,
)
from dwp_agent.operational_gate_store import PostgresOperationalGateStore
from dwp_agent.operational_gate_store_errors import (
    OperationalGateInvalidTransition,
    OperationalGateMissingEvidence,
    OperationalGateSeparationOfDutyViolation,
)
from dwp_agent.run_store import _apply_migrations


@pytest.mark.integration
def test_postgres_gate_workflow_preserves_evidence_and_approval_independence() -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Operational gate integration tests require a dedicated test database.")

    _apply_migrations(database_url)
    store = PostgresOperationalGateStore(database_url)
    tenant_id = str(800_000_000 + uuid4().int % 100_000_000)
    environment = GateEnvironment.STAGING
    gate_key = OperationalGateKey.MODEL_CREDENTIALS

    initial = store.portfolio(
        tenant_id=tenant_id,
        actor_user_id="bootstrap",
        environment=environment,
    )
    assert initial.gates == []
    idempotency_key = uuid4()
    bootstrapped = store.bootstrap(
        tenant_id=tenant_id,
        actor_user_id="bootstrap",
        correlation_id="integration-bootstrap",
        environment=environment,
        request=BootstrapOperationalGatesRequest(
            idempotency_key=idempotency_key,
            expected_existing_count=0,
            change_reason="Initialize the staging delivery gate portfolio.",
        ),
    )
    replayed = store.bootstrap(
        tenant_id=tenant_id,
        actor_user_id="bootstrap",
        correlation_id="integration-bootstrap-retry",
        environment=environment,
        request=BootstrapOperationalGatesRequest(
            idempotency_key=idempotency_key,
            expected_existing_count=0,
            change_reason="Retry the staging delivery gate initialization.",
        ),
    )
    assert replayed.total_count == bootstrapped.total_count
    with connect(database_url) as connection:
        bootstrap_events = connection.execute(
            """SELECT COUNT(*) FROM ai_governance_events
                WHERE tenant_id = %s AND category = 'GATE'
                  AND event_type = 'operational-gates.bootstrapped'""",
            (int(tenant_id),),
        ).fetchone()[0]
    assert bootstrap_events == 1

    initial = bootstrapped.gates
    gate = next(item for item in initial if item.gate_key == gate_key)

    configured = store.configure(
        tenant_id=tenant_id,
        actor_user_id="manager-a",
        correlation_id="integration-configure",
        environment=environment,
        gate_key=gate_key,
        request=ConfigureOperationalGateRequest(
            selected_option="MANAGED_IDENTITY",
            owner_user_id="owner-a",
            configuration_ref="kv://integration/model-identity",
            expected_version=gate.policy_version,
            change_reason="Configure the staging model identity control.",
        ),
    )
    assert configured.gate.status == GateStatus.CONFIGURING
    assert configured.gate.configuration_revision == 1
    assert configured.missing_evidence_types == [
        GateEvidenceType.CONFIGURATION_REFERENCE,
        GateEvidenceType.SECURITY_REVIEW,
    ]

    first_evidence = store.add_evidence(
        tenant_id=tenant_id,
        actor_user_id="manager-a",
        correlation_id="integration-evidence-config",
        environment=environment,
        gate_key=gate_key,
        request=CreateOperationalGateEvidenceRequest(
            evidence_type=GateEvidenceType.CONFIGURATION_REFERENCE,
            title="Managed identity configuration",
            reference="cmdb://integration/model-identity",
            expected_version=configured.gate.policy_version,
            change_reason="Register the immutable configuration reference.",
        ),
    )

    with pytest.raises(OperationalGateMissingEvidence) as missing:
        store.validate(
            tenant_id=tenant_id,
            actor_user_id="validator-a",
            correlation_id="integration-validation-incomplete",
            environment=environment,
            gate_key=gate_key,
            request=ValidateOperationalGateRequest(
                outcome=GateValidationOutcome.PASS,
                validation_summary="Validate the identity and security evidence set.",
                expected_version=first_evidence.gate.policy_version,
                change_reason="Attempt validation before the evidence set is complete.",
            ),
        )
    assert missing.value.missing_evidence_types == ("SECURITY_REVIEW",)

    complete_evidence = store.add_evidence(
        tenant_id=tenant_id,
        actor_user_id="security-reviewer",
        correlation_id="integration-evidence-security",
        environment=environment,
        gate_key=gate_key,
        request=CreateOperationalGateEvidenceRequest(
            evidence_type=GateEvidenceType.SECURITY_REVIEW,
            title="Security architecture review",
            reference="review://integration/security-42",
            expected_version=first_evidence.gate.policy_version,
            change_reason="Register the completed security architecture review.",
        ),
    )
    validated = store.validate(
        tenant_id=tenant_id,
        actor_user_id="validator-a",
        correlation_id="integration-validation-pass",
        environment=environment,
        gate_key=gate_key,
        request=ValidateOperationalGateRequest(
            outcome=GateValidationOutcome.PASS,
            validation_summary="Identity and security evidence satisfy the staging control.",
            expected_version=complete_evidence.gate.policy_version,
            change_reason="Record successful validation of the complete evidence set.",
        ),
    )
    assert validated.gate.status == GateStatus.READY_FOR_APPROVAL
    assert validated.missing_evidence_types == []

    validator_view = store.detail(
        tenant_id=tenant_id,
        actor_user_id="validator-a",
        environment=environment,
        gate_key=gate_key,
    )
    assert validator_view.approval_eligibility.reason == (
        GateApprovalEligibilityReason.SEPARATION_OF_DUTY
    )
    assert validator_view.approval_eligibility.conflicting_role == GateActorRole.VALIDATOR
    with pytest.raises(OperationalGateSeparationOfDutyViolation):
        store.decide(
            tenant_id=tenant_id,
            actor_user_id="validator-a",
            correlation_id="integration-self-approval",
            environment=environment,
            gate_key=gate_key,
            request=DecideOperationalGateRequest(
                decision=GateDecision.APPROVE,
                expected_version=validated.gate.policy_version,
                change_reason="Attempt approval by the validating operator.",
            ),
        )

    auditor_view = store.detail(
        tenant_id=tenant_id,
        actor_user_id="auditor-a",
        environment=environment,
        gate_key=gate_key,
    )
    assert auditor_view.approval_eligibility.eligible is True
    approved = store.decide(
        tenant_id=tenant_id,
        actor_user_id="auditor-a",
        correlation_id="integration-independent-approval",
        environment=environment,
        gate_key=gate_key,
        request=DecideOperationalGateRequest(
            decision=GateDecision.APPROVE,
            expected_version=auditor_view.gate.policy_version,
            change_reason="Approve independently after reviewing all evidence.",
        ),
    )
    assert approved.gate.status == GateStatus.APPROVED
    assert approved.gate.approved_by == "auditor-a"
    assert len(approved.evidence) == 2
    assert [event.event_type for event in approved.events] == [
        "operational-gate.approved",
        "operational-gate.validated",
        "operational-gate.evidence-added",
        "operational-gate.evidence-added",
        "operational-gate.configured",
    ]
    assert approved.events[0].current_status == GateStatus.APPROVED

    with pytest.raises(OperationalGateInvalidTransition):
        store.add_evidence(
            tenant_id=tenant_id,
            actor_user_id="manager-a",
            correlation_id="integration-evidence-after-approval",
            environment=environment,
            gate_key=gate_key,
            request=CreateOperationalGateEvidenceRequest(
                evidence_type=GateEvidenceType.OTHER,
                title="Late evidence",
                reference="evidence://integration/late",
                expected_version=approved.gate.policy_version,
                change_reason="Verify approved evidence cannot change in place.",
            ),
        )
