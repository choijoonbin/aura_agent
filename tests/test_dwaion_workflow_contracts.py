from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response
from pydantic import ValidationError

from dwp_agent.contracts import AskRequest, CitationSourceType
from dwp_agent.dwaion_workflow_api import research_capabilities
from dwp_agent.dwaion_workflow_contracts import (
    AttachmentCitation,
    AttachmentStage,
    AttachmentStageKey,
    AttachmentStageState,
    ProposalHandoffObservation,
    ProposalHandoffState,
    ResearchResult,
    ResearchRunCommandAction,
    ResearchRunCommandRequest,
)
from dwp_agent.secure_attachment_logic import derive_attachment_state
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.research_recovery_contracts import (
    ResearchRecoveryAction,
    ResearchRecoveryCommandRequest,
)


def test_ask_default_scopes_do_not_implicitly_authorize_attachments() -> None:
    request = AskRequest(requestId="scope-default", query="What needs attention?")

    assert CitationSourceType.ATTACHMENT not in request.source_scopes


def test_attachment_citation_requires_evidence_bound_digest() -> None:
    evidence = "Verified source excerpt."
    citation = AttachmentCitation(
        citationId="source-1",
        locator="page:1",
        label="Source 1",
        evidence=evidence,
        contentSha256=hashlib.sha256(evidence.encode()).hexdigest(),
    )

    assert citation.evidence == evidence
    with pytest.raises(ValidationError, match="contentSha256"):
        AttachmentCitation(
            citationId="source-1",
            locator="page:1",
            label="Source 1",
            evidence=evidence,
            contentSha256="0" * 64,
        )


def test_attachment_ready_requires_receipts_for_every_provider_stage() -> None:
    now = datetime.now(UTC)
    citation = AttachmentCitation(
        citationId="source-1",
        locator="page:1",
        label="Source 1",
        evidence="Verified source excerpt.",
        contentSha256=hashlib.sha256(b"Verified source excerpt.").hexdigest(),
    )
    stages = [
        AttachmentStage(
            key=key,
            state=(
                AttachmentStageState.NOT_REQUIRED
                if key == AttachmentStageKey.OCR
                else AttachmentStageState.PASSED
            ),
            providerCode="UPLOAD_VERIFIED" if key == AttachmentStageKey.UPLOAD else None,
            observedAt=now,
        )
        for key in AttachmentStageKey
    ]

    assert derive_attachment_state(stages, [citation], "text/plain").value == "PARTIAL"


def test_research_result_requires_report_bound_digest() -> None:
    report = "# Verified report"
    evidence = "Verified source excerpt."
    citation = AttachmentCitation(
        citationId="source-1",
        locator="page:1",
        label="Source 1",
        evidence=evidence,
        contentSha256=hashlib.sha256(evidence.encode()).hexdigest(),
    )
    result = ResearchResult(
        reportMarkdown=report,
        citations=[citation],
        resultSha256=hashlib.sha256(report.encode()).hexdigest(),
    )

    assert result.report_markdown == report
    with pytest.raises(ValidationError, match="resultSha256"):
        ResearchResult(reportMarkdown=report, citations=[citation], resultSha256="f" * 64)


def test_research_command_reasons_reject_whitespace_and_are_canonicalized() -> None:
    common = {
        "commandId": uuid4(),
        "expectedVersion": 1,
        "action": ResearchRunCommandAction.PAUSE,
    }
    with pytest.raises(ValidationError, match="reason"):
        ResearchRunCommandRequest(**common, reason="      ")

    command = ResearchRunCommandRequest(
        **common,
        reason="  Pause   while the source policy is reviewed.  ",
    )
    assert command.reason == "Pause while the source policy is reviewed."

    recovery_common = {
        "commandId": uuid4(),
        "idempotencyKey": uuid4(),
        "expectedVersion": 1,
        "action": ResearchRecoveryAction.SAVE_AS_FORK,
    }
    with pytest.raises(ValidationError, match="reason"):
        ResearchRecoveryCommandRequest(**recovery_common, reason="      ")

    recovery = ResearchRecoveryCommandRequest(
        **recovery_common,
        reason="  Preserve   the reviewed plan as a governed fork.  ",
    )
    assert recovery.reason == "Preserve the reviewed plan as a governed fork."


def test_completed_proposal_handoff_requires_a_typed_bound_domain_receipt() -> None:
    with pytest.raises(ValidationError, match="receipt"):
        ProposalHandoffObservation(
            commandId=uuid4(),
            expectedVersion=3,
            state=ProposalHandoffState.COMPLETED,
            receipt={"fabricated": True},
        )

    handoff_id = uuid4()
    proposal_id = uuid4()
    observation = ProposalHandoffObservation(
        commandId=uuid4(),
        expectedVersion=3,
        state=ProposalHandoffState.COMPLETED,
        receipt={
            "domain": "APPROVAL",
            "operation": "REQUEST_SUBMIT",
            "handoffId": handoff_id,
            "proposalId": proposal_id,
            "actionKey": "APPROVAL.REQUEST.CREATE",
            "handoffVersion": 3,
            "requestId": uuid4(),
            "requestVersion": 2,
            "status": "IN_REVIEW",
            "committedAt": datetime.now(UTC),
            "correlationId": "approval-submit-correlation",
        },
    )

    assert observation.receipt is not None
    assert observation.receipt.handoff_id == handoff_id
    with pytest.raises(ValidationError, match="timezone"):
        ProposalHandoffObservation(
            **observation.model_dump(mode="python", by_alias=True)
            | {"receipt": observation.receipt.model_dump(mode="python", by_alias=True) | {
                "committedAt": datetime.now()
            }}
        )


@pytest.mark.parametrize(
    ("receipt", "resource_field"),
    [
        (
            {
                "domain": "CALENDAR",
                "operation": "EVENT_CREATE",
                "actionKey": "CALENDAR.EVENT.CREATE",
                "eventId": uuid4(),
                "eventVersion": 0,
                "status": "CONFIRMED",
            },
            "event_id",
        ),
        (
            {
                "domain": "MAIL",
                "operation": "DRAFT_CREATE",
                "actionKey": "MAIL.DRAFT.CREATE",
                "threadId": uuid4(),
                "threadVersion": 0,
                "status": "DRAFT",
            },
            "thread_id",
        ),
        (
            {
                "domain": "SERVICE",
                "operation": "REQUEST_CREATE",
                "actionKey": "SERVICE.REQUEST.CREATE",
                "requestId": uuid4(),
                "requestVersion": 0,
                "status": "SUBMITTED",
            },
            "request_id",
        ),
    ],
)
def test_non_approval_completion_receipts_are_domain_typed(
    receipt: dict[str, object], resource_field: str
) -> None:
    handoff_id = uuid4()
    proposal_id = uuid4()
    observation = ProposalHandoffObservation(
        commandId=uuid4(),
        expectedVersion=3,
        state=ProposalHandoffState.COMPLETED,
        receipt=receipt
        | {
            "handoffId": handoff_id,
            "proposalId": proposal_id,
            "handoffVersion": 3,
            "committedAt": datetime.now(UTC),
            "correlationId": "platform-owner-completion",
        },
    )

    assert observation.receipt is not None
    assert getattr(observation.receipt, resource_field) is not None

    mismatched = observation.receipt.model_dump(mode="python", by_alias=True)
    mismatched["operation"] = "REQUEST_SUBMIT"
    with pytest.raises(ValidationError, match="literal"):
        ProposalHandoffObservation(
            commandId=uuid4(),
            expectedVersion=3,
            state=ProposalHandoffState.COMPLETED,
            receipt=mismatched,
        )


def test_research_download_capabilities_match_real_server_artifacts() -> None:
    identity = PersonalDomainIdentity(
        tenant_id=7,
        user_id="member-1",
        correlation_id="capability-check",
        auth_session_id="session-1",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset({"APP.ASK:VIEW", "APP.DWAION_RESEARCH:VIEW"}),
    )

    capabilities = research_capabilities(identity, Response()).data

    assert capabilities.raw_export.available is True
    assert capabilities.pdf_export.available is True
    assert capabilities.receipt_download.available is True
    assert capabilities.audit_download.available is True
    assert capabilities.delivery.artifact.available is False
    assert capabilities.delivery.artifact.reason_code in {
        "ARTIFACT_RETENTION_POLICY_REQUIRED",
        "RESEARCH_DELIVERY_WORKER_NOT_CONFIGURED",
        "RESEARCH_DELIVERY_WORKER_UNAVAILABLE",
    }
    assert capabilities.delivery.export.available is False
    assert capabilities.delivery.proposal.reason_code == (
        "RESEARCH_DELIVERY_WORKER_NOT_CONFIGURED"
    )
    assert capabilities.delivery.handoff.available is False
    assert capabilities.delivery.share.available is False
    assert capabilities.delivery.routine.reason_code == (
        "RESEARCH_ROUTINE_DEFINITION_REQUIRED"
    )
