from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.attachment_action_contracts import (
    AttachmentRevisionBinding,
    CreateAttachmentAuditReportRequest,
    DetachAllAttachmentsRequest,
)
from dwp_agent.attachment_audit_signing import AttachmentAuditSignature
from dwp_agent.attachment_stage_contracts import (
    AttachmentStageReceiptPayload,
    AttachmentWorkerObservation,
    build_attachment_stage_receipt,
)
from dwp_agent.dwaion_workflow_contracts import (
    AttachmentCapabilities,
    AttachmentCitation,
    AttachmentStageKey,
    AttachmentUploadTicket,
    CompleteAttachmentUploadRequest,
    CreateAttachmentRequest,
    DeleteAttachmentRequest,
    CreateResearchDeliveryRequest,
    CreateResearchPlanRequest,
    ResearchBudget,
    ResearchDeliveryState,
    ResearchDeliveryObservation,
    ResearchDeliveryType,
    ResearchPlanDefinition,
    ResearchProgress,
    ResearchResult,
    ResearchSourcePolicy,
    ResearchRunState,
    ResearchWorkerObservation,
    StartResearchRunRequest,
    WorkflowCapability,
)
from dwp_agent.dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from dwp_agent.governed_worker_runtime import (
    GovernedWorkerMaintenance,
    register_governed_worker_heartbeat,
    remove_governed_worker_heartbeat,
)
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.research_delivery_store import ResearchDeliveryStore
from dwp_agent.research_delivery_worker import PostgresResearchDeliveryWorker
from dwp_agent.research_download_store import ResearchDownloadStore
from dwp_agent.research_plan_store import ResearchPlanStore
from dwp_agent.research_recovery_contracts import ResearchRecoveryCommandRequest
from dwp_agent.research_recovery_store import ResearchRecoveryStore
from dwp_agent.research_run_store import ResearchRunStore
from dwp_agent.secure_attachment_store import SecureAttachmentStore
from dwp_agent.secure_attachment_provider import AttachmentProviderUnavailable
from dwp_agent.secure_attachment_worker import PostgresSecureAttachmentWorker
from dwp_agent.transactional_outbox import PostgresTransactionalOutboxStore


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
        pytest.fail("DWAI workflow tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


class _AttachmentProvider:
    def __init__(self) -> None:
        self.stage_calls: list[AttachmentStageKey] = []
        self.uploads: dict[object, tuple[str, int, str]] = {}

    def capabilities(self) -> AttachmentCapabilities:
        available = WorkflowCapability(available=True, configured=True)
        unavailable = WorkflowCapability(
            available=False, configured=False, reasonCode="NOT_CONFIGURED",
            recoveryHint="Configure the governed provider.",
        )
        return AttachmentCapabilities(
            upload=available, antivirus=available, dlp=available, parser=available,
            ocr=available, index=available, deletion=available,
            detachAll=unavailable, inspectionLog=available, maskingHistory=unavailable,
            ocrViewer=available, signedAuditReport=unavailable,
            maximumFileBytes=1_000_000,
            allowedMediaTypes=["text/plain", "image/png"],
        )

    def create_upload(self, **request: object) -> AttachmentUploadTicket:
        self.uploads[request["attachment_id"]] = (
            str(request["media_type"]),
            int(request["size_bytes"]),
            str(request["source_sha256"]),
        )
        return AttachmentUploadTicket(
            uploadUrl="https://uploads.example.invalid/object",
            uploadReference=f"upload-ref-{request['attachment_id']}",
            expiresAt=datetime.now(UTC) + timedelta(minutes=10),
        )

    def verify_upload(self, **request: object) -> SimpleNamespace:
        media_type, size_bytes, source_sha256 = self.uploads[request["attachment_id"]]
        return SimpleNamespace(
            uploadReference=request["upload_reference"],
            observedSizeBytes=size_bytes,
            observedSha256=source_sha256,
            observedMediaType=media_type,
        )

    def execute_stage(self, **request: object):
        stage = AttachmentStageKey(request["stage"])
        self.stage_calls.append(stage)
        citations = []
        if stage == AttachmentStageKey.PARSER:
            evidence = "The verified attachment contains the approved evidence."
            citations = [
                AttachmentCitation(
                    citationId="attachment-page-1",
                    locator="page:1",
                    label="Page 1",
                    contentSha256=hashlib.sha256(evidence.encode()).hexdigest(),
                    evidence=evidence,
                )
            ]
        return build_attachment_stage_receipt(
            AttachmentStageReceiptPayload(
                attachmentId=request["attachment_id"],
                uploadReference=request["upload_reference"],
                sourceSha256=request["source_sha256"],
                stage=stage,
                providerReceiptId=f"{stage.value.lower()}-{request['attachment_id']}",
                observedAt=datetime.now(UTC),
                verdict="PASSED",
                providerCode=f"{stage.value}_PASSED",
                citations=citations,
            )
        )


class _DeletingAttachmentProvider(_AttachmentProvider):
    def __init__(self, *, fail_attempts: int = 0) -> None:
        super().__init__()
        self.fail_attempts = fail_attempts
        self.delete_calls: list[dict[str, object]] = []

    def delete(self, **request: object) -> SimpleNamespace:
        self.delete_calls.append(request)
        if len(self.delete_calls) <= self.fail_attempts:
            raise AttachmentProviderUnavailable("ATTACHMENT_PROVIDER_UNAVAILABLE")
        return SimpleNamespace(
            uploadReference=request["upload_reference"],
            deleted=True,
            providerReceiptId=f"delete-receipt-{request['attachment_id']}",
        )


class _UnconfiguredAttachmentProvider:
    def capabilities(self) -> AttachmentCapabilities:
        unavailable = WorkflowCapability(
            available=False,
            configured=False,
            reasonCode="ATTACHMENT_PROVIDER_NOT_CONFIGURED",
            recoveryHint="Configure the governed attachment provider.",
        )
        available = WorkflowCapability(available=True, configured=True)
        return AttachmentCapabilities(
            upload=unavailable, antivirus=unavailable, dlp=unavailable,
            parser=unavailable, ocr=unavailable, index=unavailable,
            deletion=unavailable, detachAll=unavailable,
            inspectionLog=available, maskingHistory=unavailable,
            ocrViewer=unavailable, signedAuditReport=unavailable,
            maximumFileBytes=1_000_000, allowedMediaTypes=["text/plain"],
        )

    def create_upload(self, **_request: object) -> AttachmentUploadTicket:
        raise AssertionError("An unavailable upload provider must not be invoked.")

    def delete(self, **_request: object) -> SimpleNamespace:
        raise AssertionError("A missing upload reference must not reach deletion.")


class _AuditSigner:
    def capability(self, _tenant_id: int) -> WorkflowCapability:
        return WorkflowCapability(available=True, configured=True)

    def sign(self, tenant_id: int, content: bytes) -> AttachmentAuditSignature:
        digest = hashlib.sha256(str(tenant_id).encode() + b":" + content).hexdigest()
        return AttachmentAuditSignature(
            algorithm="HMAC-SHA256",
            signature=f"test-signature-{digest}",
            key_fingerprint=hashlib.sha256(
                f"attachment-test-key:{tenant_id}".encode()
            ).hexdigest(),
        )


def test_attachment_integrity_owner_scope_ready_evidence_and_tamper_block() -> None:
    identity = _identity()
    provider = _AttachmentProvider()
    register_governed_worker_heartbeat("SECURE_ATTACHMENT_PIPELINE")
    store = SecureAttachmentStore(DATABASE_URL, provider=provider)
    digest = hashlib.sha256(b"hello world\n").hexdigest()
    create = CreateAttachmentRequest(
        commandId=uuid4(), fileName="evidence.txt", mediaType="text/plain",
        sizeBytes=12, sourceSha256=digest, retentionHours=24,
    )
    attachment = store.create(identity, create)
    assert store.create(identity, create).attachment_id == attachment.attachment_id
    scanned = store.complete_upload(
        identity, attachment.attachment_id,
        CompleteAttachmentUploadRequest(
            commandId=uuid4(), expectedRevision=attachment.revision,
            uploadReference=attachment.upload_ticket.upload_reference,
            observedSizeBytes=12, observedSha256=digest,
        ),
    )
    invalid_receipt = build_attachment_stage_receipt(
        AttachmentStageReceiptPayload(
            attachmentId=attachment.attachment_id,
            uploadReference="different-upload-reference",
            sourceSha256=digest,
            stage="AV",
            providerReceiptId="misbound-av-receipt",
            observedAt=datetime.now(UTC),
            verdict="PASSED",
            providerCode="CLEAN",
        )
    )
    with pytest.raises(DwaionWorkflowConflict, match="target binding"):
        store.observe(
            identity,
            attachment.attachment_id,
            AttachmentWorkerObservation(
                commandId=uuid4(),
                expectedRevision=scanned.revision,
                receipts=[invalid_receipt],
            ),
        )
    assert store.get(identity, attachment.attachment_id).revision == scanned.revision

    outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
    lease = outbox.claim(
        tenant_id=identity.tenant_id,
        topics=("ai.secure-attachment.processing-requested.v1",),
        lease_seconds=300,
    )
    assert lease is not None
    worker = PostgresSecureAttachmentWorker(DATABASE_URL, provider=provider)
    assert worker.process(lease) == "READY"
    outbox.acknowledge(lease)
    ready = store.get(identity, attachment.attachment_id)
    assert ready.state.value == "READY"
    evidence = store.evidence(identity, ready.attachment_id)
    assert evidence.source_sha256 == digest
    assert [item.citation_id for item in evidence.citations] == ["attachment-page-1"]
    assert all(
        stage.provider_receipt_id and stage.result_digest
        for stage in ready.stages
        if stage.key in {
            AttachmentStageKey.AV,
            AttachmentStageKey.DLP,
            AttachmentStageKey.PARSER,
            AttachmentStageKey.INDEX,
        }
    )
    assert provider.stage_calls == [
        AttachmentStageKey.AV,
        AttachmentStageKey.DLP,
        AttachmentStageKey.PARSER,
        AttachmentStageKey.INDEX,
    ]
    assert [event.revision for event in evidence.inspection_log] == [1, 2, 3, 4, 5, 6]
    with pytest.raises(DwaionWorkflowNotFound):
        store.get(_identity(tenant_id=identity.tenant_id, user="other-user"), ready.attachment_id)

    tampered = store.create(
        identity,
        CreateAttachmentRequest(
            commandId=uuid4(), fileName="tampered.txt", mediaType="text/plain",
            sizeBytes=12, sourceSha256=digest, retentionHours=24,
        ),
    )
    blocked = store.complete_upload(
        identity, tampered.attachment_id,
        CompleteAttachmentUploadRequest(
            commandId=uuid4(), expectedRevision=tampered.revision,
            uploadReference=tampered.upload_ticket.upload_reference,
            observedSizeBytes=12, observedSha256="0" * 64,
        ),
    )
    assert blocked.state.value == "BLOCKED"
    remove_governed_worker_heartbeat("SECURE_ATTACHMENT_PIPELINE")


def test_attachment_delete_requires_provider_confirmation_and_is_idempotent() -> None:
    identity = _identity()
    provider = _DeletingAttachmentProvider()
    store = SecureAttachmentStore(DATABASE_URL, provider=provider)
    attachment = store.create(
        identity,
        CreateAttachmentRequest(
            commandId=uuid4(), fileName="delete-me.txt", mediaType="text/plain",
            sizeBytes=12, sourceSha256=hashlib.sha256(b"hello world\n").hexdigest(),
            retentionHours=24,
        ),
    )
    request = DeleteAttachmentRequest(
        commandId=uuid4(), expectedRevision=attachment.revision,
        reason="Remove the governed attachment from its storage provider.",
    )

    with pytest.raises(DwaionWorkflowNotFound):
        store.delete(
            _identity(tenant_id=identity.tenant_id, user="other-user"),
            attachment.attachment_id,
            request,
        )
    deleted = store.delete(identity, attachment.attachment_id, request)
    replay = store.delete(identity, attachment.attachment_id, request)

    assert deleted.state.value == "DELETED"
    assert replay == deleted
    assert deleted.deletion_attempt_count == 1
    assert deleted.deletion_last_error_code is None
    assert deleted.deletion_receipt_id == f"delete-receipt-{attachment.attachment_id}"
    assert deleted.deleted_at is not None
    assert len(provider.delete_calls) == 1
    evidence = store.evidence(identity, attachment.attachment_id)
    assert [event.event_type for event in evidence.inspection_log][-2:] == [
        "DELETE_REQUESTED",
        "DELETE_PROVIDER_CONFIRMED",
    ]
    assert evidence.deletion_attempt_count == 1
    assert evidence.deletion_receipt_id == deleted.deletion_receipt_id
    with connect(DATABASE_URL) as connection:
        upload_reference, receipt = connection.execute(
            """SELECT upload_reference_envelope, deletion_receipt_envelope
                 FROM ai_secure_attachments WHERE attachment_id = %s""",
            (attachment.attachment_id,),
        ).fetchone()
    assert upload_reference is None
    assert receipt.startswith("dwp2.")


def test_image_attachment_pipeline_requires_and_records_ocr_receipt() -> None:
    identity = _identity()
    provider = _AttachmentProvider()
    register_governed_worker_heartbeat("SECURE_ATTACHMENT_PIPELINE")
    try:
        store = SecureAttachmentStore(DATABASE_URL, provider=provider)
        content = b"image-bytes"
        digest = hashlib.sha256(content).hexdigest()
        attachment = store.create(
            identity,
            CreateAttachmentRequest(
                commandId=uuid4(),
                fileName="evidence.png",
                mediaType="image/png",
                sizeBytes=len(content),
                sourceSha256=digest,
                retentionHours=24,
            ),
        )
        scanning = store.complete_upload(
            identity,
            attachment.attachment_id,
            CompleteAttachmentUploadRequest(
                commandId=uuid4(),
                expectedRevision=attachment.revision,
                uploadReference=attachment.upload_ticket.upload_reference,
                observedSizeBytes=len(content),
                observedSha256=digest,
            ),
        )
        assert scanning.state.value == "SCANNING"
        outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
        lease = outbox.claim(
            tenant_id=identity.tenant_id,
            topics=("ai.secure-attachment.processing-requested.v1",),
            lease_seconds=300,
        )
        assert lease is not None
        assert (
            PostgresSecureAttachmentWorker(DATABASE_URL, provider=provider).process(
                lease
            )
            == "READY"
        )
        outbox.acknowledge(lease)
        ready = store.get(identity, attachment.attachment_id)
        ocr = next(
            stage for stage in ready.stages if stage.key == AttachmentStageKey.OCR
        )
        assert ocr.state.value == "PASSED"
        assert ocr.provider_receipt_id
        assert ocr.result_digest
        assert provider.stage_calls == [
            AttachmentStageKey.AV,
            AttachmentStageKey.DLP,
            AttachmentStageKey.PARSER,
            AttachmentStageKey.OCR,
            AttachmentStageKey.INDEX,
        ]
    finally:
        remove_governed_worker_heartbeat("SECURE_ATTACHMENT_PIPELINE")


def test_attachment_delete_failure_stays_pending_and_exact_replay_retries() -> None:
    identity = _identity()
    provider = _DeletingAttachmentProvider(fail_attempts=1)
    store = SecureAttachmentStore(DATABASE_URL, provider=provider)
    attachment = store.create(
        identity,
        CreateAttachmentRequest(
            commandId=uuid4(), fileName="retry-delete.txt", mediaType="text/plain",
            sizeBytes=12, sourceSha256=hashlib.sha256(b"hello world\n").hexdigest(),
            retentionHours=24,
        ),
    )
    request = DeleteAttachmentRequest(
        commandId=uuid4(), expectedRevision=attachment.revision,
        reason="Retry provider deletion without claiming premature completion.",
    )

    pending = store.delete(identity, attachment.attachment_id, request)
    assert pending.state.value == "DELETION_PENDING"
    assert pending.deletion_attempt_count == 1
    assert pending.deletion_last_error_code == "ATTACHMENT_PROVIDER_UNAVAILABLE"
    assert pending.deletion_receipt_id is None
    evidence = store.evidence(identity, attachment.attachment_id)
    assert evidence.inspection_log[-1].event_type == "DELETE_RETRY_PENDING"

    deleted = store.delete(identity, attachment.attachment_id, request)
    assert deleted.state.value == "DELETED"
    assert deleted.deletion_attempt_count == 2
    assert deleted.deletion_last_error_code is None
    assert deleted.deletion_receipt_id is not None
    assert len(provider.delete_calls) == 2


def test_unconfigured_attachment_delete_never_claims_provider_completion() -> None:
    identity = _identity()
    store = SecureAttachmentStore(
        DATABASE_URL, provider=_UnconfiguredAttachmentProvider()
    )
    attachment = store.create(
        identity,
        CreateAttachmentRequest(
            commandId=uuid4(), fileName="not-configured.txt", mediaType="text/plain",
            sizeBytes=12, sourceSha256=hashlib.sha256(b"hello world\n").hexdigest(),
            retentionHours=24,
        ),
    )
    assert attachment.state.value == "PARTIAL"
    assert attachment.upload_ticket is None
    pending = store.delete(
        identity,
        attachment.attachment_id,
        DeleteAttachmentRequest(
            commandId=uuid4(), expectedRevision=attachment.revision,
            reason="Record deletion intent while the storage provider is unavailable.",
        ),
    )

    assert pending.state.value == "DELETION_PENDING"
    assert pending.deletion_attempt_count == 1
    assert pending.deletion_receipt_id is None
    assert pending.deletion_last_error_code == (
        "ATTACHMENT_UPLOAD_REFERENCE_UNAVAILABLE"
    )
    assert pending.capabilities.deletion.available is False


def test_attachment_audit_report_and_detach_all_are_signed_scoped_and_replay_safe() -> None:
    identity = _identity()
    conversation_id = uuid4()
    store = SecureAttachmentStore(
        DATABASE_URL,
        provider=_AttachmentProvider(),
        audit_signer=_AuditSigner(),
    )
    attachments = [
        store.create(
            identity,
            CreateAttachmentRequest(
                commandId=uuid4(),
                conversationId=conversation_id,
                fileName=f"evidence-{index}.txt",
                mediaType="text/plain",
                sizeBytes=12,
                sourceSha256=hashlib.sha256(
                    f"attachment-{index}".encode()
                ).hexdigest(),
                retentionHours=24,
            ),
        )
        for index in range(2)
    ]
    bindings = [
        AttachmentRevisionBinding(
            attachmentId=item.attachment_id,
            expectedRevision=item.revision,
        )
        for item in attachments
    ]
    audit_request = CreateAttachmentAuditReportRequest(
        commandId=uuid4(),
        idempotencyKey=uuid4(),
        attachments=bindings,
        reason="Generate the signed attachment verification report before detaching.",
    )

    report = store.create_audit_report(identity, conversation_id, audit_request)
    report_replay = store.create_audit_report(
        identity, conversation_id, audit_request
    )
    report_content = store.download_audit_report(identity, report.report_id)

    assert report_replay == report
    assert set(report.attachment_ids) == {item.attachment_id for item in attachments}
    assert hashlib.sha256(report_content).hexdigest() == report.content_sha256
    assert report_content.startswith(b"%PDF")
    with pytest.raises(DwaionWorkflowNotFound):
        store.download_audit_report(
            _identity(tenant_id=identity.tenant_id, user="other-user"),
            report.report_id,
        )

    detach_request = DetachAllAttachmentsRequest(
        commandId=uuid4(),
        idempotencyKey=uuid4(),
        attachments=bindings,
        reason="Detach every verified attachment from this conversation.",
    )
    detached = store.detach_all(identity, conversation_id, detach_request)
    detached_replay = store.detach_all(identity, conversation_id, detach_request)

    assert detached_replay == detached
    assert {item.attachment_id for item in detached.detached_attachments} == {
        item.attachment_id for item in attachments
    }
    assert all(
        store.get(identity, item.attachment_id).conversation_id is None
        for item in attachments
    )
    with connect(DATABASE_URL) as connection:
        report_row = connection.execute(
            """SELECT report_envelope, receipt_envelope, signature
                 FROM ai_attachment_audit_reports WHERE report_id = %s""",
            (report.report_id,),
        ).fetchone()
        report_events = connection.execute(
            """SELECT event_type FROM ai_attachment_audit_report_events
                WHERE report_id = %s ORDER BY occurred_at, event_id""",
            (report.report_id,),
        ).fetchall()
        detach_events = connection.execute(
            """SELECT event_type FROM ai_secure_attachment_events
                WHERE attachment_id = ANY(%s)
                  AND event_type = 'DETACHED_FROM_CONVERSATION'""",
            ([item.attachment_id for item in attachments],),
        ).fetchall()

    assert report_row[0].startswith("dwp2.")
    assert report_row[1].startswith("dwp2.")
    assert report_row[2] == report.signature
    assert [row[0] for row in report_events] == ["GENERATED", "DOWNLOADED"]
    assert len(detach_events) == 2


def test_research_recovery_forks_current_plan_with_encrypted_replay_receipt() -> None:
    identity = _identity()
    plans = ResearchPlanStore(DATABASE_URL)
    definition = ResearchPlanDefinition(
        goal="Recover the reviewed research plan without losing governed evidence.",
        question="Which approved evidence supports the current operational decision?",
        successCriteria=["Every material claim includes a verified citation."],
        deliverableTypes=["REPORT", "SOURCE_MAP"],
        sourcePolicies=[
            ResearchSourcePolicy(sourceKey="WORK_ITEM", allowed=True, scope="SELF")
        ],
        requireAllAllowedSources=True,
        budget=ResearchBudget(
            maximumMinutes=20,
            maximumSources=10,
            maximumTokens=4_096,
        ),
    )
    plan = plans.create(
        identity,
        CreateResearchPlanRequest(commandId=uuid4(), definition=definition),
    )
    runs = ResearchRunStore(DATABASE_URL, plan_store=plans)
    run = runs.start(
        identity,
        plan.plan_id,
        StartResearchRunRequest(
            commandId=uuid4(),
            expectedPlanRevision=plan.revision,
            idempotencyKey=uuid4(),
        ),
    )
    partial = runs.observe(
        identity,
        run.run_id,
        ResearchWorkerObservation(
            commandId=uuid4(),
            expectedVersion=run.version,
            state="PARTIAL",
            progress=ResearchProgress(
                completedSteps=1,
                totalSteps=3,
                discoveredSources=2,
                verifiedCitations=1,
                failedSources=["WORK_ITEM"],
                recoveryHint="Save a governed fork and refresh source authorization.",
            ),
            safeErrorCode="SOURCE_REAUTH_REQUIRED",
        ),
    )
    request = ResearchRecoveryCommandRequest(
        commandId=uuid4(),
        idempotencyKey=uuid4(),
        expectedVersion=partial.version,
        action="SAVE_AS_FORK",
        reason="Preserve the current server plan as a governed recovery fork.",
    )
    recovery = ResearchRecoveryStore(DATABASE_URL)

    receipt = recovery.execute(identity, run.run_id, request)
    replay = recovery.execute(identity, run.run_id, request)

    assert replay == receipt
    assert receipt.target_plan_id is not None
    assert receipt.target_plan_revision == 1
    assert plans.get(identity, receipt.target_plan_id).definition == definition
    with pytest.raises(DwaionWorkflowNotFound):
        recovery.execute(
            _identity(tenant_id=identity.tenant_id, user="other-user"),
            run.run_id,
            request.model_copy(
                update={"command_id": uuid4(), "idempotency_key": uuid4()}
            ),
        )

    with connect(DATABASE_URL) as connection:
        command_row = connection.execute(
            """SELECT request_envelope, receipt_envelope, recovery_action
                 FROM ai_research_recovery_commands WHERE receipt_id = %s""",
            (receipt.receipt_id,),
        ).fetchone()
        event_row = connection.execute(
            """SELECT event_type, recovery_action
                 FROM ai_research_recovery_events WHERE receipt_id = %s""",
            (receipt.receipt_id,),
        ).fetchone()

    assert command_row[0].startswith("dwp2.")
    assert command_row[1].startswith("dwp2.")
    assert command_row[2] == "SAVE_AS_FORK"
    assert event_row == ("COMPLETED", "SAVE_AS_FORK")


def test_research_plan_run_partial_recovery_receipt_and_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_DEEP_RESEARCH_WORKER_ENABLED", "true")
    identity = _identity()
    plans = ResearchPlanStore(DATABASE_URL)
    definition = ResearchPlanDefinition(
        goal="Verify the current work evidence and prepare a cited decision report.",
        question="Which evidence supports the current operational decision and why?",
        successCriteria=["Every material claim has a verified citation."],
        deliverableTypes=["REPORT", "SOURCE_MAP"],
        sourcePolicies=[ResearchSourcePolicy(sourceKey="WORK_ITEM", allowed=True, scope="SELF")],
        requireAllAllowedSources=True,
        budget=ResearchBudget(maximumMinutes=20, maximumSources=10, maximumTokens=4_096),
    )
    create = CreateResearchPlanRequest(commandId=uuid4(), definition=definition)
    plan = plans.create(identity, create)
    assert plan.state.value == "READY"
    assert plans.create(identity, create).plan_id == plan.plan_id

    runs = ResearchRunStore(DATABASE_URL, plan_store=plans)
    start = StartResearchRunRequest(
        commandId=uuid4(), expectedPlanRevision=plan.revision, idempotencyKey=uuid4(),
    )
    register_governed_worker_heartbeat("RESEARCH_RUN")
    try:
        run = runs.start(identity, plan.plan_id, start)
    finally:
        remove_governed_worker_heartbeat("RESEARCH_RUN")
    assert run.state == ResearchRunState.QUEUED
    partial = runs.observe(
        identity, run.run_id,
        ResearchWorkerObservation(
            commandId=uuid4(), expectedVersion=run.version, state="PARTIAL",
            progress=ResearchProgress(
                completedSteps=1, totalSteps=3, discoveredSources=1,
                verifiedCitations=0, failedSources=["WORK_ITEM"],
                recoveryHint="Reauthorize the source and resume.",
            ), safeErrorCode="SOURCE_REAUTH_REQUIRED",
        ),
    )
    report = "Verified evidence supports the operational decision."
    citation_text = "Work item 41 is approved and ready."
    completed = runs.observe(
        identity, run.run_id,
        ResearchWorkerObservation(
            commandId=uuid4(), expectedVersion=partial.version, state="COMPLETED",
            progress=ResearchProgress(
                completedSteps=3, totalSteps=3, discoveredSources=1,
                verifiedCitations=1, failedSources=[],
            ),
            result=ResearchResult(
                reportMarkdown=report,
                citations=[AttachmentCitation(
                    citationId="work-item-41", locator="/work/items/41", label="Work item 41",
                    contentSha256=hashlib.sha256(citation_text.encode()).hexdigest(),
                    evidence=citation_text,
                )],
                resultSha256=hashlib.sha256(report.encode()).hexdigest(),
            ),
        ),
    )
    assert completed.state == ResearchRunState.COMPLETED
    assert completed.receipt_id is not None

    downloads = ResearchDownloadStore(DATABASE_URL)
    raw = downloads.raw(identity, completed.run_id)
    assert raw.result.result_sha256 == completed.result.result_sha256
    assert len(raw.integrity_fingerprint) == 64
    receipt = downloads.receipt(identity, completed.run_id)
    assert receipt.receipt_id == completed.receipt_id
    assert receipt.citation_count == 1
    audit = downloads.audit(identity, completed.run_id)
    assert {event.event_type for event in audit} >= {
        "DOWNLOAD_RAW", "DOWNLOAD_RECEIPT", "DOWNLOAD_AUDIT",
    }
    assert all(len(event.integrity_fingerprint) == 64 for event in audit)
    with pytest.raises(DwaionWorkflowNotFound):
        downloads.raw(
            _identity(tenant_id=identity.tenant_id, user="other-user"), completed.run_id,
        )

    monkeypatch.setenv("DWP_GOVERNED_WORKERS_ENABLED", "true")
    register_governed_worker_heartbeat("RESEARCH_DELIVERY")
    try:
        deliveries = ResearchDeliveryStore(DATABASE_URL, run_store=runs)
        delivery_request = CreateResearchDeliveryRequest(
            commandId=uuid4(), expectedVersion=completed.version,
            idempotencyKey=uuid4(), parameters={"format": "JSON"},
        )
        delivery = deliveries.create(
            identity, run.run_id, ResearchDeliveryType.EXPORT, delivery_request
        )
        assert deliveries.create(
            identity, run.run_id, ResearchDeliveryType.EXPORT, delivery_request
        ) == delivery
        outbox = PostgresTransactionalOutboxStore(DATABASE_URL)
        worker = PostgresResearchDeliveryWorker(DATABASE_URL)
        maintenance = GovernedWorkerMaintenance()
        delivered = delivery
        for _ in range(20):
            assert maintenance.process_once(DATABASE_URL) is True
            delivered = deliveries.get(identity, run.run_id, delivery.delivery_id)
            if delivered.receipt_id is not None:
                break
        assert delivered.receipt_id is not None
        assert delivered.receipt is not None
        assert delivered.receipt["targetType"] == "RAW_EXPORT"
        assert delivered.receipt["integrityFingerprint"] == raw.integrity_fingerprint

        exhausted_request = CreateResearchDeliveryRequest(
            commandId=uuid4(), expectedVersion=completed.version,
            idempotencyKey=uuid4(), parameters={"format": "JSON"},
        )
        exhausted_delivery = deliveries.create(
            identity, run.run_id, ResearchDeliveryType.EXPORT, exhausted_request
        )
        exhausted_lease = outbox.claim(
            tenant_id=identity.tenant_id,
            topics=("ai.research.delivery-requested.v1",),
            lease_seconds=300,
        )
        assert exhausted_lease is not None
        assert outbox.retry(
            exhausted_lease,
            safe_error_code="GOVERNED_WORKER_RETRY",
            retry_after_seconds=1,
            maximum_attempts=1,
        ) == "DEAD_LETTER"
        worker.mark_retry_exhausted(exhausted_lease)
        exhausted = deliveries.get(
            identity, run.run_id, exhausted_delivery.delivery_id
        )
        assert exhausted.state == ResearchDeliveryState.FAILED
        assert exhausted.safe_error_code == "RESEARCH_DELIVERY_RETRY_EXHAUSTED"
        assert exhausted.receipt_id is None

        monkeypatch.setenv(
            "DWP_RESEARCH_HANDOFF_PROVIDER_URL",
            "https://research-broker.internal/v1/handoffs",
        )
        monkeypatch.setenv(
            "DWP_RESEARCH_HANDOFF_PROVIDER_TOKEN",
            "research-downstream-test-token-123456",
        )
        monkeypatch.setenv(
            "DWP_RESEARCH_DOWNSTREAM_ALLOWED_HOSTS", "research-broker.internal"
        )
        handoff = deliveries.create(
            identity, run.run_id, ResearchDeliveryType.HANDOFF,
            CreateResearchDeliveryRequest(
                commandId=uuid4(), expectedVersion=completed.version,
                idempotencyKey=uuid4(), parameters={
                    "locale": "ko-KR", "approvalTarget": "finance-approvers",
                    "requestTitle": "Review verified research",
                    "requestReason": "Review the verified evidence before approval.",
                    "requestMetadata": {"priority": "NORMAL"},
                },
            ),
        )
        deliveries.observe(
            identity, run.run_id, handoff.delivery_id,
            ResearchDeliveryObservation(commandId=uuid4(), state="RUNNING"),
        )
        with pytest.raises(DwaionWorkflowConflict, match="not bound"):
            deliveries.observe(
                identity, run.run_id, handoff.delivery_id,
                ResearchDeliveryObservation(
                    commandId=uuid4(), state="COMPLETED", receiptId=uuid4(),
                    receipt={"fabricated": True},
                ),
            )
        handoff_lease = outbox.claim(
            tenant_id=identity.tenant_id,
            topics=("ai.research.delivery-requested.v1",),
            lease_seconds=300,
        )
        assert handoff_lease is not None
        assert outbox.retry(
            handoff_lease,
            safe_error_code="GOVERNED_WORKER_RETRY",
            retry_after_seconds=1,
            maximum_attempts=1,
        ) == "DEAD_LETTER"
        worker.mark_retry_exhausted(handoff_lease)

        with connect(DATABASE_URL) as connection:
            connection.execute(
                """INSERT INTO ai_domain_retention_policies (
                       tenant_id, domain_key, retention_days, deletion_grace_days,
                       legal_hold, revision, updated_by_user_id)
                   VALUES (%s, 'ARTIFACT', 30, 7, FALSE, 1, %s)
                   ON CONFLICT (tenant_id, domain_key) DO UPDATE SET
                       retention_days = EXCLUDED.retention_days,
                       updated_by_user_id = EXCLUDED.updated_by_user_id,
                       updated_at = CURRENT_TIMESTAMP""",
                (identity.tenant_id, identity.user_id),
            )
        artifact_request = CreateResearchDeliveryRequest(
            commandId=uuid4(), expectedVersion=completed.version,
            idempotencyKey=uuid4(), parameters={"locale": "en-US"},
        )
        artifact_delivery = deliveries.create(
            identity, run.run_id, ResearchDeliveryType.ARTIFACT, artifact_request
        )
        artifact_lease = outbox.claim(
            tenant_id=identity.tenant_id,
            topics=("ai.research.delivery-requested.v1",),
            lease_seconds=300,
        )
        assert artifact_lease is not None
        assert worker.process(artifact_lease) == "COMPLETED"
        outbox.acknowledge(artifact_lease)
        artifact_delivered = deliveries.get(
            identity, run.run_id, artifact_delivery.delivery_id
        )
        assert artifact_delivered.receipt is not None
        artifact_id = artifact_delivered.receipt["artifactId"]
        with connect(DATABASE_URL) as connection:
            artifact_count = connection.execute(
                """SELECT COUNT(*) FROM ai_artifacts
                    WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
                (artifact_id, identity.tenant_id, identity.user_id),
            ).fetchone()[0]
        assert artifact_count == 1

        unavailable_request = CreateResearchDeliveryRequest(
            commandId=uuid4(), expectedVersion=completed.version,
            idempotencyKey=uuid4(), parameters={"locale": "en-US"},
        )
        with pytest.raises(
            DwaionWorkflowUnavailable, match="RESEARCH_ROUTINE_DEFINITION_REQUIRED"
        ):
            deliveries.create(
                identity,
                run.run_id,
                ResearchDeliveryType.ROUTINE,
                unavailable_request,
            )
    finally:
        remove_governed_worker_heartbeat("RESEARCH_DELIVERY")


def _identity(
    tenant_id: int | None = None, *, user: str = "member-1"
) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant_id or (760_000_000 + uuid4().int % 100_000_000),
        user_id=user, correlation_id=str(uuid4()), auth_session_id="session-1",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset({
            "APP.ASK:VIEW", "APP.DWAION_ATTACHMENTS:VIEW",
            "APP.DWAION_ATTACHMENTS:MANAGE", "APP.DWAION_RESEARCH:VIEW",
            "APP.DWAION_RESEARCH:MANAGE",
        }),
    )
