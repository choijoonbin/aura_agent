from __future__ import annotations

import os
from dataclasses import replace
from io import BytesIO
from urllib.parse import urlparse
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from psycopg import connect
from psycopg.errors import RaiseException

from dwp_agent.artifact_contracts import (
    ArtifactDraftContent,
    ArtifactSourceReference,
    CreateArtifactRequest,
    CreateArtifactVersionRequest,
    ExportArtifactRequest,
    PublishArtifactRequest,
    RunArtifactPreflightRequest,
)
from dwp_agent.artifact_export_renderer import render_artifact_export
from dwp_agent.artifact_export_worker import PostgresArtifactExportWorker
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
    GovernedDomainUnavailable,
)
from dwp_agent.governed_worker_runtime import (
    MAINTENANCE,
    governed_worker_available,
)
from dwp_agent.personal_data_deletion_worker import (
    PostgresPersonalDataDeletionWorker,
)
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.personal_memory_contracts import (
    CreateMemoryRequest,
    ExplicitMemoryValue,
    UpdateMemoryPreferenceRequest,
)
from dwp_agent.personal_memory_postgres_store import PostgresPersonalMemoryStore
from dwp_agent.transactional_outbox import PostgresTransactionalOutboxStore


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)

PERMISSIONS = frozenset(
    {
        "APP.ASK:VIEW",
        "APP.DWAION_PRIVACY:VIEW",
        "APP.DWAION_PRIVACY:MANAGE",
        "APP.DWAION_MEMORY:VIEW",
        "APP.DWAION_MEMORY:MANAGE",
        "APP.DWAION_ARTIFACTS:VIEW",
        "APP.DWAION_ARTIFACTS:CREATE",
        "APP.DWAION_ARTIFACTS:UPDATE",
        "APP.DWAION_ARTIFACTS:PUBLISH",
        "APP.DWAION_ARTIFACTS:EXPORT",
        "APP.WORK:VIEW",
        "APP.MAIL:VIEW",
    }
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not DATABASE_URL:
        return
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Governed worker tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


@pytest.mark.parametrize("export_format", ("MARKDOWN", "DOCX", "PDF"))
def test_export_worker_generates_encrypted_downloadable_bytes_and_is_idempotent(
    export_format: str,
) -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ARTIFACT)
    _seed_policy(retention, identity, DomainKey.ARTIFACT_EXPORT)
    store = PostgresArtifactStore(DATABASE_URL)
    artifact = store.create(
        identity,
        CreateArtifactRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_ARTIFACT_CREATE",
            artifact_type="DOCUMENT",
            content=ArtifactDraftContent(
                title="검증된 내보내기",
                body="이 문서는 실제 파일 바이트와 무결성 증거를 검증합니다.",
            ),
        ),
    )
    version = store.create_version(
        identity,
        artifact.artifact_id,
        CreateArtifactVersionRequest(
            command_id=uuid4(),
            expected_revision=artifact.revision,
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
            version_number=version.version_number,
        ),
    )
    published = store.publish(
        identity,
        artifact.artifact_id,
        PublishArtifactRequest(
            command_id=uuid4(),
            expected_revision=preflight.artifact_revision,
            reason_code="USER_ARTIFACT_PUBLISH",
            change_reason="Publish the immutable version before governed export.",
            version_number=version.version_number,
            preflight_id=preflight.preflight_id,
        ),
    )
    pending = store.export(
        identity,
        artifact.artifact_id,
        ExportArtifactRequest(
            command_id=uuid4(),
            expected_revision=published.artifact_revision,
            reason_code="USER_ARTIFACT_EXPORT",
            change_reason="Generate a governed file inside the encrypted Agent store.",
            version_number=version.version_number,
            preflight_id=preflight.preflight_id,
            export_format=export_format,
        ),
    )
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    outbox_lease = outbox.claim(
        tenant_id=tenant,
        topics=("ai.artifact.export-requested.v1",),
        lease_seconds=300,
    )
    assert outbox_lease is not None
    worker = PostgresArtifactExportWorker(DATABASE_URL)

    assert worker.process(outbox_lease) == "SUCCEEDED"
    assert worker.process(outbox_lease) == "SUCCEEDED"
    outbox.acknowledge(outbox_lease)

    receipt = store.export_job(
        identity, artifact.artifact_id, pending.export_job_id
    )
    exported = store.export_file(
        identity, artifact.artifact_id, pending.export_job_id
    )
    assert receipt.state.value == "SUCCEEDED"
    assert receipt.file_available is True
    assert receipt.byte_size == len(exported.content)
    assert receipt.content_fingerprint == exported.content_fingerprint
    if export_format == "MARKDOWN":
        assert exported.content.decode("utf-8").startswith("# 검증된 내보내기")
    elif export_format == "DOCX":
        with ZipFile(BytesIO(exported.content)) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        assert "검증된 내보내기" in document
    else:
        assert exported.content.startswith(b"%PDF-1.7")
        assert exported.content.endswith(b"%%EOF\n")
    with connect(DATABASE_URL) as connection:
        output_count, success_count = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_artifact_export_outputs
                     WHERE export_job_id = %s),
                   (SELECT COUNT(*) FROM ai_artifact_export_events
                     WHERE export_job_id = %s AND event_type = 'SUCCEEDED')""",
            (pending.export_job_id, pending.export_job_id),
        ).fetchone()
    assert (output_count, success_count) == (1, 1)
    with pytest.raises(GovernedDomainNotFound):
        store.export_file(
            _identity(_tenant()), artifact.artifact_id, pending.export_job_id
        )
    with pytest.raises(GovernedDomainNotFound):
        store.export_file(
            replace(identity, user_id="another-user"),
            artifact.artifact_id,
            pending.export_job_id,
        )


def test_export_download_recomputes_integrity_fingerprint() -> None:
    tenant = _tenant()
    identity, store, artifact_id, export_job_id = _completed_markdown_export(tenant)
    original_codec = store.codec

    class _TamperedCodec:
        def decrypt_json(self, envelope, **values):
            payload = original_codec.decrypt_json(envelope, **values)
            if values.get("resource_type") == "artifact-export-output":
                return {"base64": "dGFtcGVyZWQ="}
            return payload

    store.codec = _TamperedCodec()
    with pytest.raises(GovernedDomainConflict, match="integrity"):
        store.export_file(identity, artifact_id, export_job_id)


def test_only_server_bound_citation_sources_receive_verification_proof() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ARTIFACT)
    store = PostgresArtifactStore(DATABASE_URL)
    source = ArtifactSourceReference(
        source_type="WORK_ITEM",
        reference=(
            "conversation:4053a568-7bd0-4bd9-a39c-ee0d6e12e51a:"
            "message:a14a615a-8a24-48dd-b45e-dc3eaed3dcf1:citation:src-01"
        ),
    )
    request = CreateArtifactRequest(
        command_id=uuid4(), expected_revision=0,
        reason_code="USER_ARTIFACT_CREATE", artifact_type="DOCUMENT",
        content=ArtifactDraftContent(title="Verified", body="Bound answer"),
        sources=[source],
    )
    request._verified_source_references = frozenset({source.reference})
    artifact = store.create(identity, request)
    version = store.create_version(
        identity, artifact.artifact_id,
        CreateArtifactVersionRequest(command_id=uuid4(), expected_revision=1, reason_code="USER_ARTIFACT_VERSION"),
    )
    detail = store.get_version(identity, artifact.artifact_id, 1)
    preflight = store.preflight(
        identity, artifact.artifact_id,
        RunArtifactPreflightRequest(command_id=uuid4(), expected_revision=version.artifact_revision, reason_code="USER_ARTIFACT_PREFLIGHT", version_number=1),
    )

    assert detail.source_evidence[0].verification_state == "SERVER_VERIFIED"
    assert detail.source_evidence[0].verified_at is not None
    assert detail.source_evidence[0].freshness == "SNAPSHOT_AT_CONVERSATION"
    assert len(detail.source_evidence[0].verification_evidence_fingerprint or "") == 64
    assert preflight.outcome.value == "PASS"
    assert artifact.capabilities.source_verification_scope == (
        "SERVER_BOUND_CONVERSATION_CITATIONS_ONLY"
    )
    assert artifact.capabilities.manual_source_verification_available is False


def test_deletion_worker_purges_active_memory_store_and_seals_one_disposition() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.MEMORY)
    memories = PostgresPersonalMemoryStore(DATABASE_URL)
    controls = memories.update_controls(
        identity,
        UpdateMemoryPreferenceRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_MEMORY_ENABLE",
            change_reason="Enable an explicit preference before deletion testing.",
            memory_state="ENABLED",
        ),
    )
    memories.create(
        identity,
        CreateMemoryRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_MEMORY_CREATE",
            kind="TONE",
            memory=ExplicitMemoryValue(value="Use concise language"),
        ),
    )
    request = retention.request_deletion(
        identity,
        RequestDeletionRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_DATA_DELETE",
            change_reason="Delete all of my active Agent memory records and envelopes.",
            domains=[DomainKey.MEMORY],
        ),
    )
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    outbox_lease = outbox.claim(
        tenant_id=tenant,
        topics=("ai.personal-data.deletion-requested.v1",),
        lease_seconds=300,
    )
    assert outbox_lease is not None
    worker = PostgresPersonalDataDeletionWorker(DATABASE_URL)

    assert worker.process(outbox_lease) == "COMPLETED"
    assert worker.process(outbox_lease) == "COMPLETED"
    outbox.acknowledge(outbox_lease)

    completed = retention.deletion_job(identity, request.deletion_job_id)
    assert completed.state.value == "COMPLETED"
    assert completed.deletion_performed is True
    assert len(completed.targets) == 1
    target = completed.targets[0]
    assert target.state.value == "COMPLETED"
    assert target.disposition is not None
    assert target.disposition.disposition_scope == "AGENT_ACTIVE_POSTGRES_DOMAIN_ONLY"
    assert target.disposition.disposition_method == (
        "PHYSICAL_ROW_PURGE_OF_ENCRYPTED_RECORDS"
    )
    assert sum(target.disposition.purged_table_counts.values()) == (
        target.disposition.purged_row_count
    )
    assert target.disposition.active_store_envelopes_destroyed is True
    assert target.disposition.source_system_data_affected is False
    assert target.disposition.backup_disposition_state == "EXTERNAL_RETENTION_BOUNDARY"
    with connect(DATABASE_URL) as connection:
        memory_rows, dispositions = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_user_memories
                     WHERE tenant_id = %s AND user_id = %s),
                   (SELECT COUNT(*) FROM ai_data_disposition_receipts
                     WHERE deletion_job_id = %s AND domain_key = 'MEMORY')""",
            (tenant, identity.user_id, request.deletion_job_id),
        ).fetchone()
    assert memory_rows == 0
    assert dispositions == 1
    with pytest.raises(GovernedDomainNotFound):
        retention.deletion_job(_identity(_tenant()), request.deletion_job_id)

    with connect(DATABASE_URL) as connection:
        connection.execute(
            "ALTER TABLE ai_data_disposition_receipts DISABLE TRIGGER "
            "trg_ai_data_disposition_receipts_append_only"
        )
        connection.execute(
            """UPDATE ai_data_disposition_receipts
                  SET receipt_fingerprint = %s
                WHERE deletion_job_id = %s""",
            ("0" * 64, request.deletion_job_id),
        )
        connection.execute(
            "ALTER TABLE ai_data_disposition_receipts ENABLE TRIGGER "
            "trg_ai_data_disposition_receipts_append_only"
        )
    with pytest.raises(GovernedDomainUnavailable):
        retention.deletion_job(identity, request.deletion_job_id)


def test_deletion_worker_rechecks_legal_hold_after_request() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    policy = _seed_policy(retention, identity, DomainKey.MEMORY)
    request = retention.request_deletion(
        identity,
        RequestDeletionRequest(
            command_id=uuid4(),
            expected_revision=0,
            reason_code="USER_DATA_DELETE",
            change_reason="Request active-store disposal before a later policy change.",
            domains=[DomainKey.MEMORY],
        ),
    )
    retention.upsert_policy(
        identity,
        DomainKey.MEMORY,
        UpsertRetentionPolicyRequest(
            command_id=uuid4(),
            expected_revision=policy.revision,
            reason_code="LEGAL_HOLD_CHANGE",
            change_reason="Preserve the requested domain under a newly approved legal hold.",
            retention_days=365,
            deletion_grace_days=7,
            legal_hold=True,
        ),
    )
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    outbox_lease = outbox.claim(
        tenant_id=tenant,
        topics=("ai.personal-data.deletion-requested.v1",),
        lease_seconds=300,
    )
    assert outbox_lease is not None

    state = PostgresPersonalDataDeletionWorker(DATABASE_URL).process(outbox_lease)
    outbox.acknowledge(outbox_lease)

    blocked = retention.deletion_job(identity, request.deletion_job_id)
    assert state == "BLOCKED_LEGAL_HOLD"
    assert blocked.state.value == "BLOCKED_LEGAL_HOLD"
    assert blocked.deletion_performed is False
    assert blocked.targets[0].disposition is None


def test_artifact_export_is_disposed_before_artifact_dependency() -> None:
    tenant = _tenant()
    identity, _, artifact_id, export_job_id = _completed_markdown_export(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    requested = retention.request_deletion(
        identity,
        RequestDeletionRequest(
            command_id=uuid4(), expected_revision=0,
            reason_code="USER_DATA_DELETE",
            change_reason="Delete generated exports before deleting their artifact versions.",
            domains=[DomainKey.ARTIFACT, DomainKey.ARTIFACT_EXPORT],
        ),
    )
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    lease = outbox.claim(
        tenant_id=tenant,
        topics=("ai.personal-data.deletion-requested.v1",),
        lease_seconds=300,
    )
    assert lease is not None

    assert PostgresPersonalDataDeletionWorker(DATABASE_URL).process(lease) == "COMPLETED"
    outbox.acknowledge(lease)

    completed = retention.deletion_job(identity, requested.deletion_job_id)
    assert {target.domain.value for target in completed.targets} == {
        "ARTIFACT",
        "ARTIFACT_EXPORT",
    }
    assert all(target.disposition is not None for target in completed.targets)
    with connect(DATABASE_URL) as connection:
        artifact_count, export_count, output_count = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_artifacts WHERE artifact_id = %s),
                   (SELECT COUNT(*) FROM ai_artifact_export_jobs WHERE export_job_id = %s),
                   (SELECT COUNT(*) FROM ai_artifact_export_outputs WHERE export_job_id = %s)""",
            (artifact_id, export_job_id, export_job_id),
        ).fetchone()
    assert (artifact_count, export_count, output_count) == (0, 0, 0)


def test_stale_deletion_generation_cannot_purge_or_write_a_receipt() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ROUTINE)
    requested = retention.request_deletion(
        identity,
        RequestDeletionRequest(
            command_id=uuid4(), expected_revision=0,
            reason_code="USER_DATA_DELETE",
            change_reason="Exercise the fenced deletion generation without stored rows.",
            domains=[DomainKey.ROUTINE],
        ),
    )
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    outbox_lease = outbox.claim(
        tenant_id=tenant,
        topics=("ai.personal-data.deletion-requested.v1",),
        lease_seconds=300,
    )
    assert outbox_lease is not None
    worker = PostgresPersonalDataDeletionWorker(DATABASE_URL)
    live = worker._claim(
        requested.deletion_job_id, tenant_id=tenant, user_id=identity.user_id
    )
    assert live is not None

    with pytest.raises(RuntimeError, match="stale or expired"):
        worker._purge_target(replace(live, generation=live.generation + 1), DomainKey.ROUTINE)

    assert worker.release_for_retry(live, safe_error_code="TEST_RETRY") is False
    outbox.retry(
        outbox_lease,
        safe_error_code="TEST_RETRY",
        retry_after_seconds=1,
    )
    with connect(DATABASE_URL) as connection:
        receipts = connection.execute(
            """SELECT COUNT(*) FROM ai_data_disposition_receipts
                WHERE deletion_job_id = %s""",
            (requested.deletion_job_id,),
        ).fetchone()[0]
    assert receipts == 0


def test_worker_capability_requires_opt_in_and_fresh_live_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    MAINTENANCE.close()
    monkeypatch.setenv("DWP_AGENT_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("DWP_GOVERNED_WORKERS_ENABLED", "true")
    assert governed_worker_available("ARTIFACT_EXPORT") is False
    assert governed_worker_available("DATA_DELETION") is False

    MAINTENANCE.start()
    try:
        assert governed_worker_available("ARTIFACT_EXPORT") is True
        assert governed_worker_available("DATA_DELETION") is True
    finally:
        MAINTENANCE.close()
    assert governed_worker_available("ARTIFACT_EXPORT") is False
    assert governed_worker_available("DATA_DELETION") is False


def test_export_worker_rejects_an_expired_preflight_at_execution_time() -> None:
    tenant = _tenant()
    identity, store, artifact_id, export_job_id = _pending_markdown_export(tenant)
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "ALTER TABLE ai_artifact_preflight_runs DISABLE TRIGGER "
            "trg_ai_artifact_preflight_runs_append_only"
        )
        connection.execute(
            """UPDATE ai_artifact_preflight_runs
                  SET created_at = CURRENT_TIMESTAMP - INTERVAL '20 minutes',
                      expires_at = CURRENT_TIMESTAMP - INTERVAL '5 minutes'
                WHERE preflight_id = (
                    SELECT preflight_id FROM ai_artifact_export_jobs
                     WHERE export_job_id = %s
                )""",
            (export_job_id,),
        )
        connection.execute(
            "ALTER TABLE ai_artifact_preflight_runs ENABLE TRIGGER "
            "trg_ai_artifact_preflight_runs_append_only"
        )
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    outbox_lease = outbox.claim(
        tenant_id=tenant,
        topics=("ai.artifact.export-requested.v1",),
        lease_seconds=300,
    )
    assert outbox_lease is not None

    assert PostgresArtifactExportWorker(DATABASE_URL).process(outbox_lease) == "FAILED"
    outbox.acknowledge(outbox_lease)

    receipt = store.export_job(identity, artifact_id, export_job_id)
    assert receipt.state.value == "FAILED"
    assert receipt.file_available is False
    assert receipt.safe_error_code == "PREFLIGHT_PROOF_INVALID"


def test_stale_export_job_lease_cannot_write_and_new_generation_completes_once() -> None:
    tenant = _tenant()
    _, _, _, export_job_id = _pending_markdown_export(tenant)
    worker = PostgresArtifactExportWorker(DATABASE_URL)
    stale = worker._claim(
        export_job_id, tenant_id=tenant, user_id="worker-user"
    )
    assert stale is not None
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """UPDATE ai_artifact_export_jobs
                  SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                WHERE export_job_id = %s""",
            (export_job_id,),
        )
    current = worker._claim(
        export_job_id, tenant_id=tenant, user_id="worker-user"
    )
    assert current is not None
    assert current.generation == stale.generation + 1

    with pytest.raises(RuntimeError, match="stale or expired"):
        worker._execute(stale)
    assert worker.retry(stale, safe_error_code="STALE_TEST") is False
    worker._execute(current)

    with connect(DATABASE_URL) as connection:
        outputs, successes = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_artifact_export_outputs
                     WHERE export_job_id = %s),
                   (SELECT COUNT(*) FROM ai_artifact_export_events
                     WHERE export_job_id = %s AND event_type = 'SUCCEEDED')""",
            (export_job_id, export_job_id),
        ).fetchone()
        connection.execute(
            """UPDATE ai_transactional_outbox SET state = 'CANCELLED'
                WHERE tenant_id = %s AND aggregate_id = %s AND state = 'PENDING'""",
            (tenant, str(export_job_id)),
        )
    assert (outputs, successes) == (1, 1)


def test_outbox_redelivery_observes_terminal_export_without_duplicate_output() -> None:
    tenant = _tenant()
    _, _, _, export_job_id = _pending_markdown_export(tenant)
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    stale = outbox.claim(
        tenant_id=tenant,
        topics=("ai.artifact.export-requested.v1",),
        lease_seconds=300,
    )
    assert stale is not None
    worker = PostgresArtifactExportWorker(DATABASE_URL)
    assert worker.process(stale) == "SUCCEEDED"
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """UPDATE ai_transactional_outbox
                  SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                WHERE outbox_id = %s""",
            (stale.outbox_id,),
        )
    with pytest.raises(GovernedDomainConflict, match="stale or expired"):
        outbox.acknowledge(stale)
    replay = outbox.claim(
        tenant_id=tenant,
        topics=("ai.artifact.export-requested.v1",),
        lease_seconds=300,
    )
    assert replay is not None
    assert replay.generation == stale.generation + 1
    assert worker.process(replay) == "SUCCEEDED"
    outbox.acknowledge(replay)

    with connect(DATABASE_URL) as connection:
        outputs, successes = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_artifact_export_outputs
                     WHERE export_job_id = %s),
                   (SELECT COUNT(*) FROM ai_artifact_export_events
                     WHERE export_job_id = %s AND event_type = 'SUCCEEDED')""",
            (export_job_id, export_job_id),
        ).fetchone()
    assert (outputs, successes) == (1, 1)


def test_export_retry_budget_reaches_an_audited_terminal_failure() -> None:
    tenant = _tenant()
    identity, store, artifact_id, export_job_id = _pending_markdown_export(tenant)
    worker = PostgresArtifactExportWorker(DATABASE_URL)
    terminal = False
    for attempt in range(1, 6):
        lease = worker._claim(
            export_job_id, tenant_id=tenant, user_id=identity.user_id
        )
        assert lease is not None
        terminal = worker.retry(lease, safe_error_code="RETRY_BUDGET_TEST")
        assert terminal is (attempt == 5)

    receipt = store.export_job(identity, artifact_id, export_job_id)
    assert receipt.state.value == "FAILED"
    assert receipt.completed_at is not None
    assert receipt.safe_error_code == "RETRY_BUDGET_TEST"
    with connect(DATABASE_URL) as connection:
        claimed, retried, failed = connection.execute(
            """SELECT
                   COUNT(*) FILTER (WHERE event_type = 'CLAIMED'),
                   COUNT(*) FILTER (WHERE event_type = 'RETRY_SCHEDULED'),
                   COUNT(*) FILTER (WHERE event_type = 'FAILED')
                 FROM ai_artifact_export_events WHERE export_job_id = %s""",
            (export_job_id,),
        ).fetchone()
        connection.execute(
            """UPDATE ai_transactional_outbox SET state = 'CANCELLED'
                WHERE tenant_id = %s AND aggregate_id = %s AND state = 'PENDING'""",
            (tenant, str(export_job_id)),
        )
    assert (claimed, retried, failed) == (5, 4, 1)


def test_disposition_context_allows_only_domain_scoped_deletes_never_updates() -> None:
    tenant = _tenant()
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.MEMORY)
    memories = PostgresPersonalMemoryStore(DATABASE_URL)
    memories.update_controls(
        identity,
        UpdateMemoryPreferenceRequest(
            command_id=uuid4(), expected_revision=0,
            reason_code="USER_MEMORY_ENABLE",
            change_reason="Create append-only evidence for the disposition guard test.",
            memory_state="ENABLED",
        ),
    )
    requested = retention.request_deletion(
        identity,
        RequestDeletionRequest(
            command_id=uuid4(), expected_revision=0,
            reason_code="USER_DATA_DELETE",
            change_reason="Open a fenced memory disposition context for guard testing.",
            domains=[DomainKey.MEMORY],
        ),
    )
    worker = PostgresPersonalDataDeletionWorker(DATABASE_URL)
    lease = worker._claim(
        requested.deletion_job_id,
        tenant_id=tenant,
        user_id=identity.user_id,
    )
    assert lease is not None
    with connect(DATABASE_URL) as connection:
        worker._set_disposition_context(connection, lease, DomainKey.MEMORY)
        with pytest.raises(RaiseException, match="append-only"):
            connection.execute(
                """UPDATE ai_user_memory_events SET event_type = event_type
                    WHERE tenant_id = %s AND user_id = %s""",
                (tenant, identity.user_id),
            )
    with connect(DATABASE_URL) as connection:
        worker._set_disposition_context(connection, lease, DomainKey.MEMORY)
        with pytest.raises(RaiseException, match="append-only"):
            connection.execute(
                """DELETE FROM ai_data_deletion_events
                    WHERE deletion_job_id = %s""",
                (requested.deletion_job_id,),
            )
    assert worker.release_for_retry(lease, safe_error_code="GUARD_TEST_DONE") is False
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """UPDATE ai_transactional_outbox SET state = 'CANCELLED'
                WHERE tenant_id = %s AND aggregate_id = %s AND state = 'PENDING'""",
            (tenant, str(requested.deletion_job_id)),
        )


def test_renderers_preserve_unicode_and_generate_real_container_formats() -> None:
    content = ArtifactDraftContent(title="업무 계획", body="첫 번째 단계\n두 번째 단계")
    markdown = render_artifact_export(content, "MARKDOWN")
    docx = render_artifact_export(content, "DOCX")
    pdf = render_artifact_export(content, "PDF")

    assert "업무 계획" in markdown.content.decode("utf-8")
    with ZipFile(BytesIO(docx.content)) as archive:
        assert "업무 계획" in archive.read("word/document.xml").decode("utf-8")
    assert pdf.content.startswith(b"%PDF-1.7")
    assert "C5C5BB34" in pdf.content.decode("latin-1")


def _completed_markdown_export(
    tenant: int,
) -> tuple[PersonalDomainIdentity, PostgresArtifactStore, UUID, UUID]:
    identity, store, artifact_id, export_job_id = _pending_markdown_export(tenant)
    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    lease = outbox.claim(
        tenant_id=tenant,
        topics=("ai.artifact.export-requested.v1",),
        lease_seconds=300,
    )
    assert lease is not None
    PostgresArtifactExportWorker(DATABASE_URL).process(lease)
    outbox.acknowledge(lease)
    return identity, store, artifact_id, export_job_id


def _pending_markdown_export(
    tenant: int,
) -> tuple[PersonalDomainIdentity, PostgresArtifactStore, UUID, UUID]:
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    _seed_policy(retention, identity, DomainKey.ARTIFACT)
    _seed_policy(retention, identity, DomainKey.ARTIFACT_EXPORT)
    store = PostgresArtifactStore(DATABASE_URL)
    artifact = store.create(
        identity,
        CreateArtifactRequest(
            command_id=uuid4(), expected_revision=0,
            reason_code="USER_ARTIFACT_CREATE", artifact_type="DOCUMENT",
            content=ArtifactDraftContent(title="Integrity", body="Original bytes"),
        ),
    )
    version = store.create_version(
        identity, artifact.artifact_id,
        CreateArtifactVersionRequest(command_id=uuid4(), expected_revision=1, reason_code="USER_ARTIFACT_VERSION"),
    )
    preflight = store.preflight(
        identity, artifact.artifact_id,
        RunArtifactPreflightRequest(command_id=uuid4(), expected_revision=version.artifact_revision, reason_code="USER_ARTIFACT_PREFLIGHT", version_number=1),
    )
    published = store.publish(
        identity, artifact.artifact_id,
        PublishArtifactRequest(command_id=uuid4(), expected_revision=preflight.artifact_revision, reason_code="USER_ARTIFACT_PUBLISH", change_reason="Publish the integrity test version.", version_number=1, preflight_id=preflight.preflight_id),
    )
    pending = store.export(
        identity, artifact.artifact_id,
        ExportArtifactRequest(command_id=uuid4(), expected_revision=published.artifact_revision, reason_code="USER_ARTIFACT_EXPORT", change_reason="Generate bytes for integrity validation.", version_number=1, preflight_id=preflight.preflight_id, export_format="MARKDOWN"),
    )
    return identity, store, artifact.artifact_id, pending.export_job_id


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
            change_reason=f"Set an explicit boundary for {domain.value.lower()} data.",
            retention_days=365,
            deletion_grace_days=7,
            legal_hold=False,
        ),
    )


def _identity(tenant: int) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant,
        user_id="worker-user",
        correlation_id=str(uuid4()),
        auth_session_id="worker-session",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=PERMISSIONS,
    )


def _tenant() -> int:
    return 930_000_000 + uuid4().int % 60_000_000
