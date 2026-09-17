from __future__ import annotations

from datetime import datetime, timezone

from .contracts import AskCitation, AskRequest, CitationSourceType
from .dwaion_workflow_contracts import AttachmentStageState, AttachmentState
from .dwaion_workflow_errors import DwaionWorkflowNotFound, DwaionWorkflowUnavailable
from .grounded_context import ContextBrokerUnavailable, GroundedContext, GroundedSource
from .personal_domain_security import PersonalDomainIdentity
from .policy import AskIdentity
from .secure_attachment_store import get_secure_attachment_store
from .workspace_authorization import WorkspaceRequestAuthorization


class AttachmentContextUnavailable(RuntimeError):
    pass


def collect_with_ready_attachments(
    broker,
    request: AskRequest,
    identity: AskIdentity,
    *,
    locale: str,
    agent_key: str,
    workspace_authorization: WorkspaceRequestAuthorization | None,
) -> GroundedContext:
    source_scopes = [
        scope for scope in request.source_scopes
        if scope != CitationSourceType.ATTACHMENT
    ]
    try:
        if source_scopes:
            context = broker.collect(
                request.query,
                identity=identity,
                locale=locale,
                agent_key=agent_key,
                source_scopes=source_scopes,
                page_context=request.page_context,
                workspace_authorization=workspace_authorization,
            )
        elif request.attachment_ids:
            context = GroundedContext(sources=(), attempted_sources=(), unavailable_sources=())
        else:
            raise ContextBrokerUnavailable("No grounded source scope is selected.")
    except ContextBrokerUnavailable:
        if not request.attachment_ids:
            raise
        context = GroundedContext(sources=(), attempted_sources=(), unavailable_sources=())
    try:
        return bind_ready_attachments(context, request, identity)
    except ContextBrokerUnavailable as error:
        raise AttachmentContextUnavailable(str(error)) from error


def bind_ready_attachments(
    context: GroundedContext,
    request: AskRequest,
    identity: AskIdentity,
) -> GroundedContext:
    if not request.attachment_ids:
        return context
    if "APP.DWAION_ATTACHMENTS:VIEW" not in {
        permission.upper() for permission in identity.permissions
    }:
        raise ContextBrokerUnavailable("Attachment access is not authorized.")
    owner = PersonalDomainIdentity(
        tenant_id=int(identity.tenant_id),
        user_id=identity.user_id,
        correlation_id=identity.correlation_id,
        auth_session_id="ask-runtime",
        roles=frozenset(role.upper() for role in identity.roles),
        permissions=frozenset(permission.upper() for permission in identity.permissions),
    )
    try:
        attachments = [
            get_secure_attachment_store().get(owner, attachment_id)
            for attachment_id in request.attachment_ids
        ]
    except (DwaionWorkflowNotFound, DwaionWorkflowUnavailable, ValueError) as error:
        raise ContextBrokerUnavailable("An attachment is unavailable.") from error
    now = datetime.now(timezone.utc)
    for attachment in attachments:
        required_stages = [
            stage
            for stage in attachment.stages
            if stage.state != AttachmentStageState.NOT_REQUIRED
        ]
        if (
            attachment.state != AttachmentState.READY
            or attachment.deleted_at is not None
            or attachment.retention_expires_at <= now
            or not required_stages
            or any(stage.state != AttachmentStageState.PASSED for stage in required_stages)
            or not attachment.citations
        ):
            raise ContextBrokerUnavailable("An attachment is not ready for grounded use.")
    available_slots = max(0, 20 - len(context.sources))
    citations = [
        (attachment, citation)
        for attachment in attachments
        for citation in attachment.citations
    ][:available_slots]
    appended = tuple(
        GroundedSource(
            citation=AskCitation(
                source_id=f"src-{len(context.sources) + index:02d}",
                source_type=CitationSourceType.ATTACHMENT,
                title=f"{attachment.file_name}: {citation.label}",
                source_system="DWAI_ON_ATTACHMENT",
                route=None,
                excerpt=citation.evidence[:500],
            ),
            evidence=citation.evidence,
            rank=len(context.sources) + index,
        )
        for index, (attachment, citation) in enumerate(citations, start=1)
    )
    if not appended:
        raise ContextBrokerUnavailable("Attachment citation evidence is unavailable.")
    return GroundedContext(
        sources=context.sources + appended,
        attempted_sources=context.attempted_sources + ("ATTACHMENT",),
        unavailable_sources=context.unavailable_sources,
        source_health=context.source_health,
        status_code=context.status_code,
    )
