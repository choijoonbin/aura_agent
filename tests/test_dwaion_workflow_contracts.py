from __future__ import annotations

import hashlib

import pytest
from fastapi import Response
from pydantic import ValidationError

from dwp_agent.contracts import AskRequest, CitationSourceType
from dwp_agent.dwaion_workflow_api import research_capabilities
from dwp_agent.dwaion_workflow_contracts import AttachmentCitation, ResearchResult
from dwp_agent.personal_domain_security import PersonalDomainIdentity


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


def test_research_download_capabilities_stay_closed_without_artifact_provider() -> None:
    identity = PersonalDomainIdentity(
        tenant_id=7,
        user_id="member-1",
        correlation_id="capability-check",
        auth_session_id="session-1",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset({"APP.ASK:VIEW", "APP.DWAION_RESEARCH:VIEW"}),
    )

    capabilities = research_capabilities(identity, Response()).data

    assert capabilities.raw_export.available is False
    assert capabilities.pdf_export.available is False
    assert capabilities.receipt_download.available is False
    assert capabilities.audit_download.available is False
