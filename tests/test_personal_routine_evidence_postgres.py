from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.domain_retention_store import PostgresDomainRetentionStore
from dwp_agent.governed_domain_contracts import (
    DomainKey,
    UpsertRetentionPolicyRequest,
)
from dwp_agent.governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
)
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.personal_memory_contracts import UpdateAiSourcePreferenceRequest
from dwp_agent.personal_memory_postgres_store import PostgresPersonalMemoryStore
from dwp_agent.personal_routine_contracts import (
    ChangeRoutineActivationRequest,
    ChangeRoutineConsentRequest,
    CreateRoutineRequest,
    RoutineDefinition,
    UpdateRoutineRequest,
)
from dwp_agent.personal_routine_evidence_contracts import (
    RollbackRoutineVersionRequest,
    TriggerRoutineWebhookRequest,
)
from dwp_agent.personal_routine_evidence_store import PersonalRoutineEvidenceStore
from dwp_agent.personal_routine_postgres_store import PostgresPersonalRoutineStore


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not DATABASE_URL:
        return
    name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Routine evidence tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


def test_webhook_versions_health_rollback_and_audited_telemetry() -> None:
    identity = _identity()
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    retention.upsert_policy(
        identity,
        DomainKey.ROUTINE,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(), expectedRevision=0,
            reasonCode="TENANT_ROUTINE_RETENTION",
            changeReason="Set the governed routine retention boundary.",
            retentionDays=365, deletionGraceDays=7, legalHold=False,
        ),
    )
    PostgresPersonalMemoryStore(DATABASE_URL).update_source_preference(
        identity,
        "WORK_ITEM",
        UpdateAiSourcePreferenceRequest(
            commandId=uuid4(), expectedRevision=0,
            reasonCode="USER_ROUTINE_SOURCE",
            changeReason="Allow work items for this explicit personal routine.",
            enabled=True,
        ),
    )
    routines = PostgresPersonalRoutineStore(
        DATABASE_URL, execution_available=lambda: True,
    )
    evidence = PersonalRoutineEvidenceStore(
        DATABASE_URL, execution_available=lambda: True,
    )
    created = routines.create(
        identity,
        CreateRoutineRequest(
            commandId=uuid4(), expectedRevision=0,
            reasonCode="USER_ROUTINE_CREATE", definition=_definition("Original"),
        ),
    )
    updated = routines.update(
        identity,
        created.routine_id,
        UpdateRoutineRequest(
            commandId=uuid4(), expectedRevision=created.revision,
            reasonCode="USER_ROUTINE_UPDATE", changeReason="Revise the routine objective.",
            definition=_definition("Revised"),
        ),
    )
    current = updated
    for scope in ("SOURCE_ACCESS", "ANALYSIS", "PROPOSAL_DELIVERY"):
        current = routines.change_consent(
            identity,
            created.routine_id,
            ChangeRoutineConsentRequest(
                commandId=uuid4(), expectedRevision=current.revision,
                reasonCode="USER_ROUTINE_CONSENT",
                changeReason="Enable this exact governed routine scope.",
                scope=scope, consentState="ENABLED",
            ),
        )
    active = routines.change_activation(
        identity,
        created.routine_id,
        ChangeRoutineActivationRequest(
            commandId=uuid4(), expectedRevision=current.revision,
            reasonCode="USER_ROUTINE_ACTIVATE",
            changeReason="Activate the consented routine for governed execution.",
            action="ACTIVATE",
        ),
    )
    webhook = TriggerRoutineWebhookRequest(
        commandId=uuid4(), expectedRevision=active.revision,
        reasonCode="USER_ROUTINE_WEBHOOK",
        changeReason="Run after the approved work item webhook event.",
        eventId=uuid4(), eventType="WORK_ITEM.APPROVED",
        occurredAt=datetime.now(UTC),
        payload={"workItemId": "WI-41", "privateValue": "never-store-plaintext"},
    )
    run = evidence.trigger_webhook(identity, active.routine_id, webhook)
    assert run.trigger.value == "WEBHOOK"
    assert evidence.trigger_webhook(identity, active.routine_id, webhook) == run
    with pytest.raises(GovernedDomainConflict):
        evidence.trigger_webhook(
            identity,
            active.routine_id,
            webhook.model_copy(
                update={"command_id": uuid4(), "event_type": "WORK_ITEM.REJECTED"}
            ),
        )
    with pytest.raises(GovernedDomainConflict):
        evidence.trigger_webhook(
            identity,
            active.routine_id,
            webhook.model_copy(update={"command_id": uuid4()}),
        )

    versions = evidence.versions(identity, active.routine_id)
    assert {item.revision for item in versions} >= {created.revision, updated.revision}
    assert all(len(item.integrity_fingerprint) == 64 for item in versions)
    health = evidence.health(identity, active.routine_id)
    assert health.state.value == "HEALTHY"
    assert health.latest_run_id == run.routine_run_id
    telemetry = evidence.telemetry(identity, active.routine_id)
    assert any(item.event_type == "EVIDENCE_DOWNLOADED" for item in telemetry)
    assert any(item.source == "EXECUTION" and item.event_type == "QUEUED" for item in telemetry)
    with pytest.raises(GovernedDomainNotFound):
        evidence.versions(_identity(tenant=identity.tenant_id, user="member-2"), active.routine_id)

    original = next(item for item in versions if item.revision == created.revision)
    rollback_request = RollbackRoutineVersionRequest(
        commandId=uuid4(), expectedRevision=active.revision,
        reasonCode="USER_ROUTINE_VERSION_ROLLBACK",
        changeReason="Restore the reviewed original routine snapshot.",
    )
    rollback_receipt_model = evidence.rollback(
        identity,
        active.routine_id,
        original.revision,
        rollback_request,
    )
    assert evidence.rollback(
        identity, active.routine_id, original.revision, rollback_request
    ) == rollback_receipt_model
    rolled_back = rollback_receipt_model.routine
    assert rollback_receipt_model.target_revision == original.revision
    assert rollback_receipt_model.target_fingerprint == original.integrity_fingerprint
    assert len(rollback_receipt_model.integrity_fingerprint) == 64
    assert rolled_back.revision == active.revision + 1
    assert rolled_back.definition.objective.startswith("Original")
    assert rolled_back.lifecycle_state.value == "PAUSED"
    rollback_version = evidence.versions(identity, active.routine_id)[0]
    assert rollback_version.command_type == "ROLLBACK"
    assert rollback_version.rollback_target_revision == original.revision
    assert rollback_version.rollback_target_fingerprint == original.integrity_fingerprint

    with connect(DATABASE_URL) as connection:
        stored = connection.execute(
            """SELECT webhook_payload_fingerprint
                 FROM ai_personal_routine_executions
                WHERE routine_run_id = %s""",
            (run.routine_run_id,),
        ).fetchone()[0]
        command_envelope = connection.execute(
            """SELECT result_envelope FROM ai_personal_routine_commands
                WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
            (identity.tenant_id, identity.user_id, webhook.command_id),
        ).fetchone()[0]
        rollback_receipt = connection.execute(
            """SELECT command_type, rollback_target_revision,
                      rollback_target_fingerprint
                 FROM ai_personal_routine_commands
                WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
            (identity.tenant_id, identity.user_id, rollback_request.command_id),
        ).fetchone()
        rollback_event = connection.execute(
            """SELECT event_type FROM ai_personal_routine_events
                WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
            (identity.tenant_id, identity.user_id, rollback_request.command_id),
        ).fetchone()
    assert len(stored) == 64
    assert "never-store-plaintext" not in command_envelope
    assert rollback_receipt == ("ROLLBACK", original.revision, original.integrity_fingerprint)
    assert rollback_event == ("VERSION_ROLLED_BACK",)


def _definition(prefix: str) -> RoutineDefinition:
    return RoutineDefinition(
        name=f"{prefix} priorities",
        objective=f"{prefix} approval-gated work proposals with cited evidence",
        triggerType="WEBHOOK", webhookEventType="WORK_ITEM.APPROVED",
        webhookEndpointReference="dwaion:routine-events",
        locale="ko-KR", sources=["WORK_ITEM"],
    )


def _identity(
    *, tenant: int | None = None, user: str = "member-1",
) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant or (850_000_000 + uuid4().int % 100_000_000),
        user_id=user, correlation_id=str(uuid4()), auth_session_id="session-1",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset({
            "APP.ASK:VIEW", "APP.DWAION_PRIVACY:VIEW",
            "APP.DWAION_PRIVACY:MANAGE", "APP.DWAION_MEMORY:VIEW",
            "APP.DWAION_MEMORY:MANAGE", "APP.DWAION_ROUTINES:VIEW",
            "APP.DWAION_ROUTINES:MANAGE", "APP.WORK:VIEW",
        }),
    )
