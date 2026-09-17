from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.dwaion_workflow_contracts import (
    AttachmentCapabilities,
    AttachmentCitation,
    AttachmentStage,
    AttachmentStageKey,
    AttachmentStageState,
    AttachmentUploadTicket,
    AttachmentWorkerObservation,
    CompleteAttachmentUploadRequest,
    CreateAttachmentRequest,
    CreateResearchDeliveryRequest,
    CreateResearchPlanRequest,
    ResearchBudget,
    ResearchDeliveryObservation,
    ResearchDeliveryState,
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
from dwp_agent.dwaion_workflow_errors import DwaionWorkflowNotFound
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.research_delivery_store import ResearchDeliveryStore
from dwp_agent.research_download_store import ResearchDownloadStore
from dwp_agent.research_plan_store import ResearchPlanStore
from dwp_agent.research_run_store import ResearchRunStore
from dwp_agent.secure_attachment_store import SecureAttachmentStore


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
            maximumFileBytes=1_000_000, allowedMediaTypes=["text/plain"],
        )

    def create_upload(self, **request: object) -> AttachmentUploadTicket:
        return AttachmentUploadTicket(
            uploadUrl="https://uploads.example.invalid/object",
            uploadReference=f"upload-ref-{request['attachment_id']}",
            expiresAt=datetime.now(UTC) + timedelta(minutes=10),
        )

    def verify_upload(self, **request: object) -> SimpleNamespace:
        return SimpleNamespace(
            uploadReference=request["upload_reference"],
            observedSizeBytes=12,
            observedSha256=hashlib.sha256(b"hello world\n").hexdigest(),
            observedMediaType="text/plain",
        )


def test_attachment_integrity_owner_scope_ready_evidence_and_tamper_block() -> None:
    identity = _identity()
    store = SecureAttachmentStore(DATABASE_URL, provider=_AttachmentProvider())
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
    citation_text = "The verified attachment contains the approved evidence."
    citation = AttachmentCitation(
        citationId="attachment-page-1", locator="page:1", label="Page 1",
        contentSha256=hashlib.sha256(citation_text.encode()).hexdigest(),
        evidence=citation_text,
    )
    ready = store.observe(
        identity, attachment.attachment_id,
        AttachmentWorkerObservation(
            commandId=uuid4(), expectedRevision=scanned.revision,
            stages=_passed_text_stages(), citations=[citation],
        ),
    )
    assert ready.state.value == "READY"
    evidence = store.evidence(identity, ready.attachment_id)
    assert evidence.source_sha256 == digest
    assert evidence.citations == [citation]
    assert [event.revision for event in evidence.inspection_log] == [1, 2, 3]
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
    run = runs.start(identity, plan.plan_id, start)
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

    deliveries = ResearchDeliveryStore(DATABASE_URL, run_store=runs)
    delivery_request = CreateResearchDeliveryRequest(
        commandId=uuid4(), expectedVersion=completed.version,
        idempotencyKey=uuid4(), parameters={"format": "JSON"},
    )
    delivery = deliveries.create(identity, run.run_id, ResearchDeliveryType.EXPORT, delivery_request)
    assert deliveries.create(identity, run.run_id, ResearchDeliveryType.EXPORT, delivery_request) == delivery
    running = deliveries.observe(
        identity, run.run_id, delivery.delivery_id,
        ResearchDeliveryObservation(commandId=uuid4(), state=ResearchDeliveryState.RUNNING),
    )
    receipt_id = uuid4()
    delivered = deliveries.observe(
        identity, run.run_id, running.delivery_id,
        ResearchDeliveryObservation(
            commandId=uuid4(), state=ResearchDeliveryState.COMPLETED,
            receiptId=receipt_id, receipt={"objectRef": "export:verified-41"},
        ),
    )
    assert delivered.receipt_id == receipt_id
    assert deliveries.list(identity, run.run_id)[0] == delivered


def _passed_text_stages() -> list[AttachmentStage]:
    return [
        AttachmentStage(
            key=key,
            state=(AttachmentStageState.NOT_REQUIRED if key == AttachmentStageKey.OCR
                   else AttachmentStageState.PASSED),
            observedAt=datetime.now(UTC),
        )
        for key in AttachmentStageKey
    ]


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
