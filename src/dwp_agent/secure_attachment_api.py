from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Response, status

from .attachment_action_contracts import (
    AttachmentAuditReportReceiptEnvelope,
    AttachmentDetachReceiptEnvelope,
    CreateAttachmentAuditReportRequest,
    DetachAllAttachmentsRequest,
)
from .attachment_evidence_contracts import AttachmentEvidenceEnvelope
from .dwaion_workflow_api_support import (
    prevent_response_storage,
    require_attachment_access,
    run_workflow,
)
from .dwaion_workflow_contracts import (
    AttachmentCapabilitiesEnvelope,
    AttachmentEnvelope,
    AttachmentListEnvelope,
    CompleteAttachmentUploadRequest,
    CreateAttachmentRequest,
    DeleteAttachmentRequest,
)
from .personal_domain_security import (
    PersonalDomainIdentity,
    personal_domain_dependencies,
    require_personal_domain_identity,
)
from .secure_attachment_store import get_secure_attachment_store


router = APIRouter(dependencies=personal_domain_dependencies)


@router.get(
    "/v1/attachments/capabilities",
    response_model=AttachmentCapabilitiesEnvelope,
    tags=["attachments"],
)
def attachment_capabilities(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentCapabilitiesEnvelope:
    require_attachment_access(identity, write=False)
    prevent_response_storage(response)
    return AttachmentCapabilitiesEnvelope(
        data=run_workflow(lambda: get_secure_attachment_store().capabilities(identity))
    )


@router.post(
    "/v1/attachments/conversations/{conversation_id}/detach-all",
    response_model=AttachmentDetachReceiptEnvelope,
    tags=["attachments"],
)
def detach_conversation_attachments(
    conversation_id: UUID,
    request: DetachAllAttachmentsRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentDetachReceiptEnvelope:
    require_attachment_access(identity, write=True)
    prevent_response_storage(response)
    return AttachmentDetachReceiptEnvelope(
        data=run_workflow(
            lambda: get_secure_attachment_store().detach_all(
                identity, conversation_id, request
            )
        )
    )


@router.post(
    "/v1/attachments/conversations/{conversation_id}/audit-reports",
    response_model=AttachmentAuditReportReceiptEnvelope,
    tags=["attachments"],
)
def create_attachment_audit_report(
    conversation_id: UUID,
    request: CreateAttachmentAuditReportRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentAuditReportReceiptEnvelope:
    require_attachment_access(identity, write=True)
    prevent_response_storage(response)
    return AttachmentAuditReportReceiptEnvelope(
        data=run_workflow(
            lambda: get_secure_attachment_store().create_audit_report(
                identity, conversation_id, request
            )
        )
    )


@router.get(
    "/v1/attachments/audit-reports/{report_id}/download",
    tags=["attachments"],
)
def download_attachment_audit_report(
    report_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
) -> Response:
    require_attachment_access(identity, write=False)
    content = run_workflow(
        lambda: get_secure_attachment_store().download_audit_report(identity, report_id)
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="dwaion-attachment-audit-{report_id}.pdf"'
            ),
        },
    )


@router.get(
    "/v1/attachments", response_model=AttachmentListEnvelope, tags=["attachments"]
)
def list_attachments(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentListEnvelope:
    require_attachment_access(identity, write=False)
    prevent_response_storage(response)
    return AttachmentListEnvelope(
        data=run_workflow(lambda: get_secure_attachment_store().list(identity))
    )


@router.post(
    "/v1/attachments",
    status_code=status.HTTP_201_CREATED,
    response_model=AttachmentEnvelope,
    tags=["attachments"],
)
def create_attachment(
    request: CreateAttachmentRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEnvelope:
    require_attachment_access(identity, write=True)
    prevent_response_storage(response)
    return AttachmentEnvelope(
        data=run_workflow(lambda: get_secure_attachment_store().create(identity, request))
    )


@router.get(
    "/v1/attachments/{attachment_id}",
    response_model=AttachmentEnvelope,
    tags=["attachments"],
)
def get_attachment(
    attachment_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEnvelope:
    require_attachment_access(identity, write=False)
    prevent_response_storage(response)
    return AttachmentEnvelope(
        data=run_workflow(lambda: get_secure_attachment_store().get(identity, attachment_id))
    )


@router.get(
    "/v1/attachments/{attachment_id}/evidence",
    response_model=AttachmentEvidenceEnvelope,
    tags=["attachments"],
)
def get_attachment_evidence(
    attachment_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEvidenceEnvelope:
    require_attachment_access(identity, write=False)
    prevent_response_storage(response)
    return AttachmentEvidenceEnvelope(
        data=run_workflow(
            lambda: get_secure_attachment_store().evidence(identity, attachment_id)
        )
    )


@router.post(
    "/v1/attachments/{attachment_id}/complete",
    response_model=AttachmentEnvelope,
    tags=["attachments"],
)
def complete_attachment(
    attachment_id: UUID,
    request: CompleteAttachmentUploadRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEnvelope:
    require_attachment_access(identity, write=True)
    prevent_response_storage(response)
    return AttachmentEnvelope(
        data=run_workflow(
            lambda: get_secure_attachment_store().complete_upload(
                identity, attachment_id, request
            )
        )
    )


@router.delete(
    "/v1/attachments/{attachment_id}",
    response_model=AttachmentEnvelope,
    tags=["attachments"],
)
def delete_attachment(
    attachment_id: UUID,
    request: Annotated[DeleteAttachmentRequest, Body()],
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEnvelope:
    require_attachment_access(identity, write=True)
    prevent_response_storage(response)
    return AttachmentEnvelope(
        data=run_workflow(
            lambda: get_secure_attachment_store().delete(identity, attachment_id, request)
        )
    )
