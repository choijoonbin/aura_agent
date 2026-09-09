from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError, connect

from dwp_agent.artifact_contracts import (
    ArtifactDraftContent,
    ArtifactSourceReference,
    AutosaveArtifactRequest,
    CreateArtifactRequest,
    CreateArtifactVersionRequest,
    ExportArtifactRequest,
    PublishArtifactRequest,
    RunArtifactPreflightRequest,
)
from dwp_agent.artifact_postgres_store import PostgresArtifactStore
from dwp_agent.database_migrations import apply_migrations
from dwp_agent.domain_retention_store import PostgresDomainRetentionStore
from dwp_agent.governed_domain_contracts import (
    DomainKey,
    RequestDeletionRequest,
    UpsertRetentionPolicyRequest,
)
from dwp_agent.governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
)
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.personal_memory_contracts import (
    CreateMemoryRequest,
    DeleteMemoryRequest,
    ExplicitMemoryValue,
    MemoryState,
    RuntimeMemorySelection,
    UpdateAiSourcePreferenceRequest,
    UpdateMemoryPreferenceRequest,
    UpdateMemoryRuntimePreferenceRequest,
    UpdateMemoryRequest,
)
from dwp_agent.personal_memory_postgres_store import PostgresPersonalMemoryStore
from dwp_agent.personal_routine_contracts import (
    ChangeRoutineConsentRequest,
    ChangeRoutineLifecycleRequest,
    CreateRoutineRequest,
    DryRunRoutineRequest,
    RoutineDefinition,
    UpdateRoutineRequest,
)
from dwp_agent.personal_routine_postgres_store import PostgresPersonalRoutineStore
from dwp_agent.transactional_outbox import PostgresTransactionalOutboxStore


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)

ALL_PERMISSIONS = frozenset(
    {
        "APP.ASK:VIEW",
        "APP.DWAION_PRIVACY:VIEW",
        "APP.DWAION_PRIVACY:MANAGE",
        "APP.DWAION_ROUTINES:VIEW",
        "APP.DWAION_ROUTINES:MANAGE",
        "APP.DWAION_MEMORY:VIEW",
        "APP.DWAION_MEMORY:MANAGE",
        "APP.DWAION_ARTIFACTS:VIEW",
        "APP.DWAION_ARTIFACTS:CREATE",
        "APP.DWAION_ARTIFACTS:UPDATE",
        "APP.DWAION_ARTIFACTS:PUBLISH",
        "APP.DWAION_ARTIFACTS:EXPORT",
        "APP.WORK:VIEW",
        "APP.MAIL:VIEW",
        "APP.CALENDAR:VIEW",
        "APP.APPROVALS:VIEW",
    }
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Personal-domain tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)
    _truncate()
    yield
    _truncate()


def test_migrations_apply_through_v34_with_database_invariants() -> None:
    with connect(DATABASE_URL) as connection:
        versions = connection.execute(
            "SELECT version FROM sys_schema_history ORDER BY installed_at"
        ).fetchall()
        routine_constraints = connection.execute(
            """SELECT pg_get_constraintdef(oid)
                 FROM pg_constraint
                WHERE conrelid = 'ai_personal_routines'::regclass"""
        ).fetchall()
        worker_tables = connection.execute(
            """SELECT to_regclass('ai_artifact_export_outputs'),
                      to_regclass('ai_artifact_export_events'),
                      to_regclass('ai_data_disposition_receipts')"""
        ).fetchone()

    assert {row[0] for row in versions} >= {
        "V23", "V24", "V25", "V26", "V27", "V28", "V29", "V30", "V31", "V32", "V33", "V34"
    }
    assert worker_tables is not None and all(worker_tables)
    definitions = " ".join(row[0] for row in routine_constraints)
    assert "DRY_RUN_ONLY" in definitions
    assert "next_run_at IS NULL" in definitions
    assert "CASE" in definitions
    assert "source_access_consent_state" in definitions


def test_routine_consent_dry_run_reconsent_encryption_and_isolation() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    memory = PostgresPersonalMemoryStore(DATABASE_URL)
    routines = PostgresPersonalRoutineStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ROUTINE)
    source_command = UpdateAiSourcePreferenceRequest(
        command_id=uuid4(),
        expected_revision=0,
        reason_code="USER_SOURCE_CONSENT",
        change_reason="Use my work items only for explicit personal routine previews.",
        enabled=True,
    )
    memory.update_source_preference(identity, "WORK_ITEM", source_command)
    source_controls = memory.controls(identity)
    work_source = next(
        item for item in source_controls.source_preferences
        if item.source_key.value == "WORK_ITEM"
    )
    assert work_source.available is True
    assert work_source.enabled is True
    assert work_source.effective is True
    create_request = CreateRoutineRequest(
        command_id=uuid4(),
        expected_revision=0,
        reason_code="USER_ROUTINE_CREATE",
        definition=_routine_definition("Morning priorities"),
    )

    created = routines.create(identity, create_request)
    replayed = routines.create(identity, create_request)

    assert replayed.routine_id == created.routine_id
    assert created.execution_mode == "DRY_RUN_ONLY"
    assert created.scheduling_available is False
    consented = created
    for scope in ("SOURCE_ACCESS", "ANALYSIS", "PROPOSAL_DELIVERY"):
        consented = routines.change_consent(
            identity,
            created.routine_id,
            ChangeRoutineConsentRequest(
                command_id=uuid4(),
                expected_revision=consented.revision,
                reason_code="USER_ROUTINE_CONSENT",
                change_reason="Allow this exact routine scope for an on-demand preview.",
                scope=scope,
                consent_state="ENABLED",
            ),
        )
    assert consented.consent_state.value == "ENABLED"
    assert consented.capabilities.activation_available is False
    assert consented.capabilities.background_execution_available is False
    assert consented.capabilities.proposal_delivery_available is False
    paused = routines.change_lifecycle(
        identity,
        created.routine_id,
        ChangeRoutineLifecycleRequest(
            command_id=uuid4(),
            expected_revision=consented.revision,
            reason_code="USER_ROUTINE_PAUSE",
            change_reason="Pause all on-demand previews while I review this routine.",
            action="PAUSE",
        ),
    )
    assert paused.lifecycle_state.value == "PAUSED"
    with pytest.raises(GovernedDomainConflict):
        routines.dry_run(
            identity,
            created.routine_id,
            DryRunRoutineRequest(
                command_id=uuid4(),
                expected_revision=paused.revision,
                reason_code="USER_ROUTINE_DRY_RUN",
            ),
        )
    consented = routines.change_lifecycle(
        identity,
        created.routine_id,
        ChangeRoutineLifecycleRequest(
            command_id=uuid4(),
            expected_revision=paused.revision,
            reason_code="USER_ROUTINE_RESUME",
            change_reason="Resume manual preview access after reviewing this routine.",
            action="RESUME",
        ),
    )
    dry_run = routines.dry_run(
        identity,
        created.routine_id,
        DryRunRoutineRequest(
            command_id=uuid4(),
            expected_revision=consented.revision,
            reason_code="USER_ROUTINE_DRY_RUN",
            reference_time=datetime(2026, 9, 4, 0, 0, tzinfo=UTC),
        ),
    )
    assert dry_run.proposal_only is True
    assert dry_run.external_writes_performed == 0
    assert dry_run.proposals_created == 0
    assert dry_run.outcome == "VALIDATED"
    assert dry_run.evidence_count == 1
    assert dry_run.business_evidence_count == 0
    assert dry_run.evaluated_at is not None
    updated = routines.update(
        identity,
        created.routine_id,
        UpdateRoutineRequest(
            command_id=uuid4(),
            expected_revision=consented.revision,
            reason_code="USER_ROUTINE_EDIT",
            definition=_routine_definition("Changed scope"),
        ),
    )
    assert updated.consent_state.value == "RECONSENT_REQUIRED"
    with pytest.raises(GovernedDomainConflict):
        routines.dry_run(
            identity,
            created.routine_id,
            DryRunRoutineRequest(
                command_id=uuid4(),
                expected_revision=updated.revision,
                reason_code="USER_ROUTINE_DRY_RUN",
            ),
        )
    with pytest.raises(GovernedDomainNotFound):
        routines.get(_identity(tenant, user="member-2"), created.routine_id)
    with connect(DATABASE_URL) as connection:
        envelope, next_run_at, mode = connection.execute(
            """SELECT definition_envelope, next_run_at, execution_mode
                 FROM ai_personal_routines WHERE routine_id = %s""",
            (created.routine_id,),
        ).fetchone()
    assert envelope.startswith("dwp2.")
    assert "Morning priorities" not in envelope
    assert next_run_at is None
    assert mode == "DRY_RUN_ONLY"


def test_concurrent_routine_create_replays_one_canonical_result() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    memory = PostgresPersonalMemoryStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ROUTINE)
    memory.update_source_preference(
        identity,
        "WORK_ITEM",
        UpdateAiSourcePreferenceRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_SOURCE_CONSENT",
            change_reason="Use work items for this explicit routine preview.",
            enabled=True,
        ),
    )
    request = CreateRoutineRequest(
        command_id=uuid4(),
        expected_revision=0,
        reason_code="USER_ROUTINE_CREATE",
        definition=_routine_definition("Canonical concurrent routine"),
    )
    barrier = Barrier(3)

    def create_once():
        barrier.wait()
        return PostgresPersonalRoutineStore(DATABASE_URL).create(identity, request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create_once) for _ in range(2)]
        barrier.wait()
        results = [future.result() for future in futures]

    assert results[0].routine_id == results[1].routine_id
    with connect(DATABASE_URL) as connection:
        routine_count, command_count, event_count = connection.execute(
            """SELECT
                 (SELECT COUNT(*) FROM ai_personal_routines
                   WHERE tenant_id = %s AND user_id = %s),
                 (SELECT COUNT(*) FROM ai_personal_routine_commands
                   WHERE tenant_id = %s AND user_id = %s AND command_id = %s),
                 (SELECT COUNT(*) FROM ai_personal_routine_events
                   WHERE tenant_id = %s AND user_id = %s AND command_id = %s)""",
            (
                tenant,
                identity.user_id,
                tenant,
                identity.user_id,
                request.command_id,
                tenant,
                identity.user_id,
                request.command_id,
            ),
        ).fetchone()
    assert (routine_count, command_count, event_count) == (1, 1, 1)


def test_memory_is_explicit_idempotent_tombstoned_and_session_bound() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    store = PostgresPersonalMemoryStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.MEMORY)
    initial_controls = store.controls(identity)
    assert initial_controls.memory_state.value == "UNSET"
    assert initial_controls.revision == 0
    with connect(DATABASE_URL) as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM ai_user_memory_preferences
                WHERE tenant_id = %s AND user_id = %s""",
            (tenant, identity.user_id),
        ).fetchone()[0] == 0
    create = CreateMemoryRequest(
        command_id=uuid4(),
        expected_revision=0,
        reason_code="USER_MEMORY_CREATE",
        kind="OUTPUT_FORMAT",
        memory=ExplicitMemoryValue(value="Use concise numbered lists"),
    )
    with pytest.raises(GovernedDomainConflict):
        store.create(identity, create)
    controls = store.update_controls(
        identity,
        UpdateMemoryPreferenceRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_MEMORY_CONSENT",
            change_reason="Remember only preferences that I explicitly enter and can delete.",
            memory_state="ENABLED",
        ),
    )
    assert controls.memory_enabled is True
    assert controls.memory_effective is False
    assert controls.runtime_application_state.value == "UNSET"
    assert controls.runtime_application_available is True
    assert controls.team_memory_available is False
    assert controls.automatic_memory_inference is False
    created = store.create(identity, create)
    assert store.create(identity, create).memory_id == created.memory_id
    with pytest.raises(GovernedDomainConflict):
        store.create(_identity(tenant, session="session-2"), create)
    deleted = store.delete(
        identity,
        created.memory_id,
        DeleteMemoryRequest(
            command_id=uuid4(),
            expected_revision=1,
            reason_code="USER_MEMORY_DELETE",
            change_reason="Remove this preference from all future DWAI-ON context.",
        ),
    )
    assert deleted.state.value == "DELETED"
    assert store.list(identity) == []
    with connect(DATABASE_URL) as connection:
        envelope = connection.execute(
            "SELECT payload_envelope FROM ai_user_memories WHERE memory_id = %s",
            (created.memory_id,),
        ).fetchone()[0]
    assert envelope.startswith("dwp2.")
    assert "concise numbered" not in envelope.lower()


def test_runtime_memory_requires_separate_consent_and_is_owner_expiry_scoped() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    other = replace(identity, user_id="member-2")
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    store = PostgresPersonalMemoryStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.MEMORY)

    for owner in (identity, other):
        store.update_controls(
            owner,
            UpdateMemoryPreferenceRequest(
                command_id=uuid4(),
                expected_revision=0,
                reason_code="USER_MEMORY_ENABLE",
                change_reason="Store only explicit presentation preferences.",
                memory_state="ENABLED",
            ),
        )
        runtime_command = UpdateMemoryRuntimePreferenceRequest(
            command_id=uuid4(),
            expected_revision=1,
            reason_code="USER_RUNTIME_PERSONALIZATION",
            change_reason="Apply active explicit presentation preferences to my answers.",
            runtime_application_state="ENABLED",
        )
        enabled = store.update_runtime_controls(owner, runtime_command)
        assert store.update_runtime_controls(owner, runtime_command) == enabled
        with pytest.raises(GovernedDomainConflict):
            store.update_runtime_controls(replace(owner, auth_session_id="other-session"), runtime_command)

    old_tone = store.create(
        identity,
        CreateMemoryRequest(
            command_id=uuid4(), expected_revision=0, reason_code="USER_MEMORY_CREATE",
            kind="TONE", memory=ExplicitMemoryValue(value="Use a warm tone"),
        ),
    )
    store.create(
        identity,
        CreateMemoryRequest(
            command_id=uuid4(), expected_revision=0, reason_code="USER_MEMORY_CREATE",
            kind="TONE", memory=ExplicitMemoryValue(value="Use a concise professional tone"),
        ),
    )
    expired = store.create(
        identity,
        CreateMemoryRequest(
            command_id=uuid4(), expected_revision=0, reason_code="USER_MEMORY_CREATE",
            kind="OUTPUT_FORMAT", memory=ExplicitMemoryValue(value="Use tables"),
        ),
    )
    store.create(
        other,
        CreateMemoryRequest(
            command_id=uuid4(), expected_revision=0, reason_code="USER_MEMORY_CREATE",
            kind="WORKING_STYLE", memory=ExplicitMemoryValue(value="Other user's preference"),
        ),
    )
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE ai_user_memories SET retention_until = CURRENT_TIMESTAMP - INTERVAL '1 day' WHERE memory_id = %s",
            (expired.memory_id,),
        )
        connection.execute(
            "UPDATE ai_user_memories SET memory_state = %s WHERE memory_id = %s",
            (MemoryState.DISABLED.value, old_tone.memory_id),
        )

    selection = store.runtime_preferences(tenant_id=tenant, user_id=identity.user_id)

    assert selection.storage_enabled is True
    assert selection.runtime_enabled is True
    assert [(item.kind.value, item.memory.value) for item in selection.memories] == [
        ("TONE", "Use a concise professional tone")
    ]

    disabled = store.update_controls(
        identity,
        UpdateMemoryPreferenceRequest(
            command_id=uuid4(),
            expected_revision=2,
            reason_code="USER_MEMORY_DISABLE",
            change_reason="Stop storing and applying my explicit preferences.",
            memory_state="DISABLED",
        ),
    )
    assert disabled.runtime_application_state.value == "DISABLED"
    assert disabled.memory_effective is False
    reenabled = store.update_controls(
        identity,
        UpdateMemoryPreferenceRequest(
            command_id=uuid4(),
            expected_revision=3,
            reason_code="USER_MEMORY_REENABLE",
            change_reason="Resume storage without silently restoring answer application.",
            memory_state="ENABLED",
        ),
    )
    assert reenabled.runtime_application_state.value == "DISABLED"
    assert reenabled.memory_effective is False
    assert store.runtime_preferences(
        tenant_id=tenant, user_id=identity.user_id
    ) == RuntimeMemorySelection(True, False, ())


def test_sensitive_memory_is_rejected_without_persisting_command_or_event() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    store = PostgresPersonalMemoryStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.MEMORY)
    store.update_controls(
        identity,
        UpdateMemoryPreferenceRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_MEMORY_ENABLE",
            change_reason="Enable explicit non-sensitive work preference storage.",
            memory_state="ENABLED",
        ),
    )
    command_id = uuid4()

    with pytest.raises(GovernedDomainConflict):
        store.create(
            identity,
            CreateMemoryRequest(
                command_id=command_id,
                expected_revision=0,
                reason_code="USER_MEMORY_CREATE",
                kind="WORKING_STYLE",
                memory=ExplicitMemoryValue(value="api_key = exposed-secret"),
            ),
        )

    with connect(DATABASE_URL) as connection:
        counts = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_user_memories
                     WHERE tenant_id = %s AND user_id = %s),
                   (SELECT COUNT(*) FROM ai_user_memory_commands
                     WHERE tenant_id = %s AND user_id = %s AND command_id = %s),
                   (SELECT COUNT(*) FROM ai_user_memory_events
                     WHERE tenant_id = %s AND user_id = %s AND command_id = %s)""",
            (
                tenant,
                identity.user_id,
                tenant,
                identity.user_id,
                command_id,
                tenant,
                identity.user_id,
                command_id,
            ),
        ).fetchone()
    assert counts == (0, 0, 0)


def test_artifact_version_preflight_publish_export_is_fail_closed_and_truthful() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    store = PostgresArtifactStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ARTIFACT)
    _seed_policy(retention, identity, DomainKey.ARTIFACT_EXPORT)
    artifact = store.create(
        identity,
        CreateArtifactRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_ARTIFACT_CREATE",
            artifact_type="WORK_PLAN",
            content=ArtifactDraftContent(
                title="Release plan",
                body="Review the approved release checklist before deployment.",
            ),
        ),
    )
    with pytest.raises(GovernedDomainConflict):
        store.publish(
            identity,
            artifact.artifact_id,
            PublishArtifactRequest(
                command_id=uuid4(),
                expected_revision=1,
                reason_code="USER_ARTIFACT_PUBLISH",
                change_reason="Publish the reviewed release plan for my workspace.",
                version_number=1,
                preflight_id=uuid4(),
            ),
        )
    version = store.create_version(
        identity,
        artifact.artifact_id,
        CreateArtifactVersionRequest(
            command_id=uuid4(), expected_revision=1, reason_code="USER_ARTIFACT_VERSION"
        ),
    )
    summaries = store.list_versions(
        identity, artifact.artifact_id, limit=20, before_version=None
    )
    version_detail = store.get_version(
        identity, artifact.artifact_id, version.version_number
    )
    assert [item.version_number for item in summaries] == [1]
    assert version_detail.content.title == "Release plan"
    assert version_detail.source_evidence == []
    assert version_detail.immutable is True
    with pytest.raises(GovernedDomainNotFound):
        store.get_version(
            _identity(_tenant()), artifact.artifact_id, version.version_number
        )
    preflight = store.preflight(
        identity,
        artifact.artifact_id,
        RunArtifactPreflightRequest(
            command_id=uuid4(),
            expected_revision=version.artifact_revision,
            reason_code="USER_ARTIFACT_PREFLIGHT",
            version_number=version.version_number,
        ),
    )
    assert preflight.outcome.value == "PASS"
    current_preflight = store.current_preflight(identity, artifact.artifact_id)
    assert current_preflight.preflight_id == preflight.preflight_id
    assert current_preflight.current is True
    assert current_preflight.publish_allowed is True
    published = store.publish(
        identity,
        artifact.artifact_id,
        PublishArtifactRequest(
            command_id=uuid4(),
            expected_revision=preflight.artifact_revision,
            reason_code="USER_ARTIFACT_PUBLISH",
            change_reason="Publish the immutable version that passed deterministic DLP.",
            version_number=version.version_number,
            preflight_id=preflight.preflight_id,
        ),
    )
    export_request = ExportArtifactRequest(
        command_id=uuid4(),
        expected_revision=published.artifact_revision,
        reason_code="USER_ARTIFACT_EXPORT",
        change_reason="Prepare a governed PDF export request for this published version.",
        version_number=version.version_number,
        preflight_id=preflight.preflight_id,
        export_format="PDF",
    )
    export = store.export(identity, artifact.artifact_id, export_request)
    replay = store.export(identity, artifact.artifact_id, export_request)
    assert replay.export_job_id == export.export_job_id
    assert export.state == "PENDING"
    assert export.file_available is False
    assert export.external_write_performed is False
    with connect(DATABASE_URL) as connection:
        draft_envelope, version_envelope, output_reference, outbox_envelope = connection.execute(
            """SELECT d.content_envelope, v.content_envelope,
                      x.output_reference_envelope, o.payload_envelope
                 FROM ai_artifact_drafts d
                 JOIN ai_artifact_versions v USING (artifact_id)
                 JOIN ai_artifact_export_jobs x USING (artifact_id, version_number)
                 JOIN ai_transactional_outbox o
                   ON o.aggregate_id = x.export_job_id::text
                WHERE d.artifact_id = %s""",
            (artifact.artifact_id,),
        ).fetchone()
    assert draft_envelope.startswith("dwp2.")
    assert version_envelope.startswith("dwp2.")
    assert "Release plan" not in draft_envelope
    assert output_reference is None
    assert outbox_envelope.startswith("dwp2.")
    with connect(DATABASE_URL) as connection:
        with pytest.raises(PsycopgError):
            connection.execute(
                """UPDATE ai_artifact_versions SET source_count = 1
                    WHERE artifact_id = %s AND version_number = %s""",
                (artifact.artifact_id, version.version_number),
            )
        connection.rollback()

def test_unverified_artifact_source_and_dlp_secret_cannot_publish() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    store = PostgresArtifactStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ARTIFACT)
    for body, sources, expected in (
        (
            "Use the approved mail thread as evidence.",
            [ArtifactSourceReference(source_type="MAIL", reference="mail:thread-77")],
            "REVIEW",
        ),
        ("api_key = exposed-secret", [], "BLOCKED"),
    ):
        artifact = store.create(
            identity,
            CreateArtifactRequest(
                command_id=uuid4(),
                expected_revision=0,
                reason_code="USER_ARTIFACT_CREATE",
                artifact_type="DOCUMENT",
                content=ArtifactDraftContent(title="Controlled draft", body=body),
                sources=sources,
            ),
        )
        version = store.create_version(
            identity,
            artifact.artifact_id,
            CreateArtifactVersionRequest(
                command_id=uuid4(),
                expected_revision=1,
                reason_code="USER_ARTIFACT_VERSION",
            ),
        )
        preflight = store.preflight(
            identity,
            artifact.artifact_id,
            RunArtifactPreflightRequest(
                command_id=uuid4(),
                expected_revision=version.artifact_revision,
                reason_code="USER_ARTIFACT_PREFLIGHT",
                version_number=1,
            ),
        )
        assert preflight.outcome.value == expected
        detail = store.get_version(identity, artifact.artifact_id, 1)
        if sources:
            assert detail.source_evidence[0].verification_state == "UNVERIFIED"
            assert detail.source_evidence[0].freshness == "UNKNOWN"
            assert detail.source_evidence[0].verified_at is None
            with connect(DATABASE_URL) as connection:
                with pytest.raises(PsycopgError):
                    connection.execute(
                        """UPDATE ai_artifact_version_sources
                              SET verification_state = 'UNVERIFIED'
                            WHERE artifact_id = %s AND version_number = %s""",
                        (artifact.artifact_id, version.version_number),
                    )
                connection.rollback()
            with connect(DATABASE_URL) as connection:
                connection.execute(
                    """INSERT INTO ai_artifact_version_sources (
                           source_link_id, artifact_id, version_number, source_type,
                           reference_fingerprint, reference_envelope,
                           verification_state)
                       VALUES (%s, %s, %s, 'MAIL', %s, 'dwp2.invalid',
                               'UNVERIFIED')""",
                    (
                        uuid4(),
                        artifact.artifact_id,
                        version.version_number,
                        "f" * 64,
                    ),
                )
                with pytest.raises(PsycopgError):
                    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
                connection.rollback()
        with pytest.raises(GovernedDomainConflict):
            store.publish(
                identity,
                artifact.artifact_id,
                PublishArtifactRequest(
                    command_id=uuid4(),
                    expected_revision=preflight.artifact_revision,
                    reason_code="USER_ARTIFACT_PUBLISH",
                    change_reason="Attempt publication only after a passing preflight result.",
                    version_number=1,
                    preflight_id=preflight.preflight_id,
                ),
            )


def test_optimistic_autosave_allows_only_one_concurrent_writer() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    store = PostgresArtifactStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ARTIFACT)
    artifact = store.create(
        identity,
        CreateArtifactRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_ARTIFACT_CREATE",
            artifact_type="DOCUMENT",
            content=ArtifactDraftContent(title="Draft", body="Initial content"),
        ),
    )
    barrier = Barrier(3)

    def autosave(index: int):
        barrier.wait()
        try:
            return store.autosave(
                identity,
                artifact.artifact_id,
                AutosaveArtifactRequest(
                    command_id=uuid4(),
                    expected_revision=1,
                    reason_code="USER_ARTIFACT_AUTOSAVE",
                    content=ArtifactDraftContent(
                        title="Draft", body=f"Concurrent content {index}"
                    ),
                ),
            )
        except GovernedDomainConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(autosave, index) for index in range(2)]
        barrier.wait()
        results = [future.result() for future in futures]
    assert sum(isinstance(result, GovernedDomainConflict) for result in results) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1


def test_deletion_request_respects_legal_hold_and_never_claims_completion() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    store = PostgresDomainRetentionStore(DATABASE_URL)
    _seed_policy(store, identity, DomainKey.ROUTINE)
    memory_policy = _seed_policy(store, identity, DomainKey.MEMORY)
    store.upsert_policy(
        identity,
        DomainKey.MEMORY,
        UpsertRetentionPolicyRequest(
            command_id=uuid4(),
            expected_revision=memory_policy.revision,
            reason_code="LEGAL_HOLD_CHANGE",
            change_reason="Preserve memory records while the approved legal hold remains active.",
            retention_days=365,
            deletion_grace_days=7,
            legal_hold=True,
        ),
    )
    blocked = store.request_deletion(
        identity,
        RequestDeletionRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_DATA_DELETE",
            change_reason="Delete my personal AI memory data unless a legal hold blocks it.",
            domains=[DomainKey.MEMORY],
        ),
    )
    assert blocked.state.value == "BLOCKED_LEGAL_HOLD"
    assert blocked.deletion_performed is False
    request = RequestDeletionRequest(
        command_id=uuid4(),
        expected_revision=0,
        reason_code="USER_DATA_DELETE",
        change_reason="Request deletion of my personal routine data under the active policy.",
        domains=[DomainKey.ROUTINE],
    )
    pending = store.request_deletion(identity, request)
    assert store.request_deletion(identity, request).deletion_job_id == pending.deletion_job_id
    assert pending.state.value == "REQUESTED"
    assert pending.deletion_performed is False
    with connect(DATABASE_URL) as connection:
        outbox_count = connection.execute(
            """SELECT COUNT(*) FROM ai_transactional_outbox
                WHERE tenant_id = %s AND aggregate_id = %s""",
            (tenant, str(pending.deletion_job_id)),
        ).fetchone()[0]
    assert outbox_count == 1
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    barrier = Barrier(3)

    def claim_once():
        barrier.wait()
        return outbox.claim(
            tenant_id=tenant,
            topics=("ai.personal-data.deletion-requested.v1",),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim_once) for _ in range(2)]
        barrier.wait()
        claims = [future.result() for future in futures]
    leases = [claim for claim in claims if claim is not None]
    assert len(leases) == 1
    lease = leases[0]
    with pytest.raises(GovernedDomainConflict):
        outbox.acknowledge(replace(lease, generation=lease.generation + 1))
    outbox.acknowledge(lease)
    with connect(DATABASE_URL) as connection:
        state, raw_token_count = connection.execute(
            """SELECT o.state,
                      COUNT(*) FILTER (
                          WHERE e.lease_token_fingerprint = %s)
                 FROM ai_transactional_outbox o
                 JOIN ai_transactional_outbox_events e USING (outbox_id)
                WHERE o.outbox_id = %s GROUP BY o.state""",
            (str(lease.lease_token), lease.outbox_id),
        ).fetchone()
        with pytest.raises(PsycopgError):
            connection.execute(
                """UPDATE ai_transactional_outbox_events SET generation = 9
                    WHERE outbox_id = %s""",
                (lease.outbox_id,),
            )
        connection.rollback()
    assert state == "DELIVERED"
    assert raw_token_count == 0


def _seed_policy(
    store: PostgresDomainRetentionStore,
    identity: PersonalDomainIdentity,
    domain: DomainKey,
):
    return store.upsert_policy(
        identity,
        domain,
        UpsertRetentionPolicyRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="TENANT_RETENTION_BOOTSTRAP",
            change_reason=f"Set an explicit retention boundary for {domain.value.lower()} data.",
            retention_days=365,
            deletion_grace_days=7,
            legal_hold=False,
        ),
    )


def _routine_definition(name: str) -> RoutineDefinition:
    return RoutineDefinition(
        name=name,
        objective="Preview my current work priorities without executing any action",
        cadence="WEEKDAYS",
        local_time="09:00",
        time_zone="Asia/Seoul",
        locale="ko-KR",
        sources=["WORK_ITEM"],
    )


def _identity(
    tenant: int,
    *,
    user: str = "member-1",
    session: str = "session-1",
) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant,
        user_id=user,
        correlation_id=str(uuid4()),
        auth_session_id=session,
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=ALL_PERMISSIONS,
    )


def _tenant() -> int:
    return 820_000_000 + uuid4().int % 100_000_000


def _truncate() -> None:
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """TRUNCATE TABLE
                   ai_artifact_events, ai_artifact_commands,
                   ai_artifact_export_events, ai_artifact_export_outputs,
                   ai_artifact_export_jobs, ai_artifact_preflight_runs,
                   ai_artifact_version_sources, ai_artifact_versions,
                   ai_artifact_draft_sources, ai_artifact_drafts, ai_artifacts,
                   ai_personal_routine_events, ai_personal_routine_runs,
                   ai_personal_routine_consents, ai_personal_routine_commands,
                   ai_personal_routine_sources, ai_personal_routines,
                   ai_user_memory_events, ai_user_memory_commands,
                   ai_user_memories, ai_user_ai_source_preferences,
                   ai_user_memory_preferences, ai_transactional_outbox_events,
                   ai_transactional_outbox,
                   ai_data_deletion_events, ai_data_deletion_targets,
                   ai_data_disposition_receipts,
                   ai_data_deletion_jobs, ai_domain_retention_events,
                   ai_domain_retention_policies CASCADE"""
        )
