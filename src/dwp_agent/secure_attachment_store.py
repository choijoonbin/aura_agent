from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .attachment_evidence_contracts import AttachmentEvidence, AttachmentEvidenceEvent
from .dwaion_workflow_contracts import (
    AttachmentCapabilities,
    AttachmentCitation,
    AttachmentStage,
    AttachmentStageKey,
    AttachmentStageState,
    AttachmentState,
    CreateAttachmentRequest,
    SecureAttachment,
    WorkflowCapability,
)
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec, advisory_lock
from .governed_worker_runtime import governed_worker_available
from .personal_domain_security import PersonalDomainIdentity
from .secure_attachment_provider import (
    AttachmentProviderUnavailable,
    SecureAttachmentProvider,
)
from .secure_attachment_deletion import SecureAttachmentDeletionCommands
from .secure_attachment_actions import SecureAttachmentGovernedActions
from .secure_attachment_logic import (
    derive_attachment_state,
    initial_attachment_stages,
    matches_attachment_create,
    safe_attachment_error,
)
from .secure_attachment_pipeline import (
    ATTACHMENT_PIPELINE_TOPIC,
    SecureAttachmentPipelineCommands,
)
from .attachment_audit_signing import (
    AttachmentAuditSigningProvider,
    EnvironmentAttachmentAuditSigningProvider,
)
from .transactional_outbox import enqueue_internal_intent


class SecureAttachmentStore(
    SecureAttachmentPipelineCommands,
    SecureAttachmentDeletionCommands,
    SecureAttachmentGovernedActions,
):
    def __init__(
        self,
        database_url: str,
        provider: SecureAttachmentProvider | None = None,
        audit_signer: AttachmentAuditSigningProvider | None = None,
    ) -> None:
        self.database_url = database_url
        self.provider = provider or SecureAttachmentProvider()
        self.audit_signer = audit_signer or EnvironmentAttachmentAuditSigningProvider()
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise DwaionWorkflowUnavailable("Attachment encryption is unavailable.") from error

    def capabilities(
        self, identity: PersonalDomainIdentity | None = None
    ) -> AttachmentCapabilities:
        capabilities = self.provider.capabilities()
        if not governed_worker_available("SECURE_ATTACHMENT_PIPELINE"):
            worker_configured = (
                os.getenv("DWP_GOVERNED_WORKERS_ENABLED", "false")
                .strip()
                .lower()
                == "true"
            )
            unavailable = WorkflowCapability(
                available=False,
                configured=worker_configured,
                reason_code=(
                    "ATTACHMENT_PIPELINE_WORKER_UNAVAILABLE"
                    if worker_configured
                    else "ATTACHMENT_PIPELINE_WORKER_NOT_CONFIGURED"
                ),
                recovery_hint=(
                    "Restore the governed secure attachment pipeline worker."
                    if worker_configured
                    else "Enable the governed worker runtime before uploading attachments."
                ),
            )
            capabilities = capabilities.model_copy(
                update={
                    name: unavailable
                    for name in ("antivirus", "dlp", "parser", "ocr", "index")
                    if getattr(capabilities, name).available
                }
            )
        signing = (
            self.audit_signer.capability(identity.tenant_id)
            if identity is not None
            else WorkflowCapability(
                available=False,
                configured=False,
                reason_code="ATTACHMENT_AUDIT_SIGNING_IDENTITY_REQUIRED",
                recovery_hint="Resolve the tenant identity before checking signing capability.",
            )
        )
        return capabilities.model_copy(
            update={
                "detach_all": WorkflowCapability(available=True, configured=True),
                "signed_audit_report": signing,
            }
        )

    def create(
        self, identity: PersonalDomainIdentity, request: CreateAttachmentRequest
    ) -> SecureAttachment:
        capabilities = self.capabilities(identity)
        if request.media_type.lower() not in capabilities.allowed_media_types:
            raise DwaionWorkflowConflict("ATTACHMENT_MEDIA_TYPE_BLOCKED")
        if request.size_bytes > capabilities.maximum_file_bytes:
            raise DwaionWorkflowConflict("ATTACHMENT_SIZE_LIMIT_EXCEEDED")
        descriptor = {
            "fileName": request.file_name,
            "mediaType": request.media_type.lower(),
        }
        stages = initial_attachment_stages(capabilities, request.media_type.lower())
        initial_state = (
            AttachmentState.UPLOADING
            if capabilities.upload.available
            else AttachmentState.PARTIAL
        )
        attachment_id = uuid4()
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(connection, "attachment-command", identity.tenant_id, identity.user_id, request.command_id)
                existing = connection.execute(
                    _SELECT + " WHERE a.tenant_id = %s AND a.user_id = %s AND a.command_id = %s",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if existing is not None:
                    current = self._record(existing, capabilities)
                    if not matches_attachment_create(current, request):
                        raise DwaionWorkflowConflict("The attachment command ID is already in use.")
                    return current
                envelope = self.codec.encrypt_json(
                    descriptor,
                    tenant_id=identity.tenant_id,
                    resource_type="secure-attachment",
                    resource_id=str(attachment_id),
                    field="descriptor",
                )
                stage_envelope = self._stage_envelope(identity, attachment_id, stages)
                row = connection.execute(
                    """INSERT INTO ai_secure_attachments (
                           attachment_id, tenant_id, user_id, conversation_id,
                           command_id, attachment_state, size_bytes, source_sha256,
                           descriptor_envelope, stage_results_envelope,
                           retention_expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               CURRENT_TIMESTAMP + (%s * INTERVAL '1 hour'))
                    RETURNING *""",
                    (
                        attachment_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.conversation_id,
                        request.command_id,
                        initial_state.value,
                        request.size_bytes,
                        request.source_sha256,
                        envelope,
                        stage_envelope,
                        request.retention_hours,
                    ),
                ).fetchone()
                fingerprint = self._request_fingerprint(identity, "create", request)
                self._event(connection, identity, row, request.command_id, "CREATED", None, fingerprint)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Attachment storage is unavailable.") from error
        if initial_state == AttachmentState.PARTIAL:
            return self.get(identity, attachment_id)
        try:
            ticket = self.provider.create_upload(
                attachment_id=attachment_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                media_type=request.media_type.lower(),
                size_bytes=request.size_bytes,
                source_sha256=request.source_sha256,
                correlation_id=identity.correlation_id,
            )
            self._store_upload_reference(identity, attachment_id, ticket.upload_reference)
            return self.get(identity, attachment_id).model_copy(update={"upload_ticket": ticket})
        except AttachmentProviderUnavailable as error:
            self._mark_provider_failure(identity, attachment_id, str(error))
            return self.get(identity, attachment_id)

    def list(self, identity: PersonalDomainIdentity) -> list[SecureAttachment]:
        capabilities = self.capabilities(identity)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    _SELECT + " WHERE a.tenant_id = %s AND a.user_id = %s ORDER BY a.updated_at DESC",
                    (identity.tenant_id, identity.user_id),
                ).fetchall()
                return [self._record(row, capabilities) for row in rows]
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Attachment storage is unavailable.") from error

    def get(self, identity: PersonalDomainIdentity, attachment_id: UUID) -> SecureAttachment:
        capabilities = self.capabilities(identity)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    _SELECT + " WHERE a.attachment_id = %s AND a.tenant_id = %s AND a.user_id = %s",
                    (attachment_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if row is None:
                    raise DwaionWorkflowNotFound("The secure attachment is unavailable.")
                return self._record(row, capabilities)
        except DwaionWorkflowNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Attachment storage is unavailable.") from error

    def evidence(
        self, identity: PersonalDomainIdentity, attachment_id: UUID
    ) -> AttachmentEvidence:
        attachment = self.get(identity, attachment_id)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """SELECT event_id, event_type, previous_state, current_state,
                              revision, safe_error_code, occurred_at
                         FROM ai_secure_attachment_events
                        WHERE attachment_id = %s AND tenant_id = %s AND user_id = %s
                        ORDER BY occurred_at, event_id""",
                    (attachment_id, identity.tenant_id, identity.user_id),
                ).fetchall()
            events = [AttachmentEvidenceEvent.model_validate(dict(row)) for row in rows]
            ocr_passed = any(
                stage.key == AttachmentStageKey.OCR
                and stage.state == AttachmentStageState.PASSED
                for stage in attachment.stages
            )
            return AttachmentEvidence(
                attachment_id=attachment.attachment_id,
                source_sha256=attachment.source_sha256,
                deletion_attempt_count=attachment.deletion_attempt_count,
                deletion_last_error_code=attachment.deletion_last_error_code,
                deletion_receipt_id=attachment.deletion_receipt_id,
                stages=attachment.stages,
                citations=attachment.citations,
                inspection_log=events,
                masking_history=[event for event in events if "MASK" in event.event_type],
                ocr_evidence=attachment.citations if ocr_passed else [],
            )
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Attachment evidence is unavailable.") from error

    def _transition(
        self,
        identity: PersonalDomainIdentity,
        attachment_id: UUID,
        command_id: UUID,
        expected_revision: int,
        state: AttachmentState,
        event_type: str,
        fingerprint: str,
        *,
        safe_error_code: str | None = None,
        stages: list[AttachmentStage] | None = None,
        citations: list[AttachmentCitation] | None = None,
        pipeline_payload: dict[str, object] | None = None,
    ) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                _SELECT + " WHERE a.attachment_id = %s AND a.tenant_id = %s AND a.user_id = %s FOR UPDATE",
                (attachment_id, identity.tenant_id, identity.user_id),
            ).fetchone()
            if row is None:
                raise DwaionWorkflowNotFound("The secure attachment is unavailable.")
            replay = connection.execute(
                "SELECT request_fingerprint FROM ai_secure_attachment_events WHERE tenant_id = %s AND user_id = %s AND command_id = %s",
                (identity.tenant_id, identity.user_id, command_id),
            ).fetchone()
            if replay is not None:
                if replay["request_fingerprint"] != fingerprint:
                    raise DwaionWorkflowConflict("The attachment command ID is already in use.")
                return
            if int(row["revision"]) != expected_revision:
                raise DwaionWorkflowConflict("The attachment revision has changed.")
            stage_envelope = self._stage_envelope(identity, attachment_id, stages) if stages is not None else row["stage_results_envelope"]
            citation_envelope = self._citation_envelope(identity, attachment_id, citations) if citations is not None else row["citation_manifest_envelope"]
            updated = connection.execute(
                """UPDATE ai_secure_attachments
                      SET attachment_state = %s, revision = revision + 1,
                          stage_results_envelope = %s, citation_manifest_envelope = %s,
                          deleted_at = CASE WHEN %s = 'DELETED' THEN CURRENT_TIMESTAMP ELSE deleted_at END,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE attachment_id = %s
                RETURNING *""",
                (state.value, stage_envelope, citation_envelope, state.value, attachment_id),
            ).fetchone()
            self._event(connection, identity, updated, command_id, event_type, row["attachment_state"], fingerprint, safe_error_code)
            if pipeline_payload is not None:
                enqueue_internal_intent(
                    connection,
                    codec=self.codec,
                    fingerprints=self.fingerprints,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    topic=ATTACHMENT_PIPELINE_TOPIC,
                    aggregate_type="SECURE_ATTACHMENT_PIPELINE",
                    aggregate_id=str(attachment_id),
                    payload={
                        **pipeline_payload,
                        "attachmentRevision": int(updated["revision"]),
                    },
                    retention_until=updated["retention_expires_at"],
                )

    def _replay_or_conflict(self, identity: PersonalDomainIdentity, attachment_id: UUID, command_id: UUID, fingerprint: str) -> SecureAttachment:
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = connection.execute(
                "SELECT request_fingerprint FROM ai_secure_attachment_events WHERE tenant_id = %s AND user_id = %s AND command_id = %s",
                (identity.tenant_id, identity.user_id, command_id),
            ).fetchone()
        if replay is None or replay["request_fingerprint"] != fingerprint:
            raise DwaionWorkflowConflict("The attachment state or command binding has changed.")
        return self.get(identity, attachment_id)

    def _record(self, row: Any, capabilities: AttachmentCapabilities) -> SecureAttachment:
        descriptor = self.codec.decrypt_json(
            row["descriptor_envelope"], tenant_id=row["tenant_id"],
            resource_type="secure-attachment", resource_id=str(row["attachment_id"]), field="descriptor",
        )
        stages = self.codec.decrypt_json(
            row["stage_results_envelope"], tenant_id=row["tenant_id"],
            resource_type="secure-attachment", resource_id=str(row["attachment_id"]), field="stages",
        )
        citations = (
            self.codec.decrypt_json(
                row["citation_manifest_envelope"], tenant_id=row["tenant_id"],
                resource_type="secure-attachment", resource_id=str(row["attachment_id"]), field="citations",
            )
            if row["citation_manifest_envelope"] else {"items": []}
        )
        deletion_receipt = (
            self.codec.decrypt_json(
                row["deletion_receipt_envelope"], tenant_id=row["tenant_id"],
                resource_type="secure-attachment", resource_id=str(row["attachment_id"]),
                field="deletion-receipt",
            )
            if row["deletion_receipt_envelope"] else None
        )
        stored_state = AttachmentState(row["attachment_state"])
        derived_state = derive_attachment_state(
            [AttachmentStage.model_validate(item) for item in stages["items"]],
            [AttachmentCitation.model_validate(item) for item in citations["items"]],
            descriptor["mediaType"],
        )
        projected_state = (
            derived_state
            if stored_state == AttachmentState.READY
            and derived_state != AttachmentState.READY
            else stored_state
        )
        return SecureAttachment(
            attachment_id=row["attachment_id"], conversation_id=row["conversation_id"],
            file_name=descriptor["fileName"], media_type=descriptor["mediaType"],
            size_bytes=row["size_bytes"], source_sha256=row["source_sha256"],
            revision=row["revision"], state=projected_state,
            stages=[AttachmentStage.model_validate(item) for item in stages["items"]],
            citations=[AttachmentCitation.model_validate(item) for item in citations["items"]],
            retention_expires_at=row["retention_expires_at"], capabilities=capabilities,
            created_at=row["created_at"], updated_at=row["updated_at"], deleted_at=row["deleted_at"],
            deletion_attempt_count=row["deletion_attempt_count"],
            deletion_last_error_code=row["deletion_last_error_code"],
            deletion_receipt_id=(
                str(deletion_receipt["providerReceiptId"])
                if deletion_receipt and deletion_receipt.get("providerReceiptId")
                else None
            ),
        )

    def _stage_envelope(self, identity: PersonalDomainIdentity, attachment_id: UUID, stages: list[AttachmentStage]) -> str:
        return self.codec.encrypt_json(
            {"items": [item.model_dump(mode="json", by_alias=True) for item in stages]},
            tenant_id=identity.tenant_id, resource_type="secure-attachment",
            resource_id=str(attachment_id), field="stages",
        )

    def _citation_envelope(self, identity: PersonalDomainIdentity, attachment_id: UUID, citations: list[AttachmentCitation]) -> str:
        return self.codec.encrypt_json(
            {"items": [item.model_dump(mode="json", by_alias=True) for item in citations]},
            tenant_id=identity.tenant_id, resource_type="secure-attachment",
            resource_id=str(attachment_id), field="citations",
        )

    def _request_fingerprint(self, identity: PersonalDomainIdentity, purpose: str, request: Any) -> str:
        return self.fingerprints.value(
            tenant_id=identity.tenant_id, purpose=f"secure-attachment:{purpose}",
            payload=request.model_dump(mode="json", by_alias=True),
        )

    def _store_upload_reference(self, identity: PersonalDomainIdentity, attachment_id: UUID, reference: str) -> None:
        envelope = self.codec.encrypt_json(
            {"uploadReference": reference}, tenant_id=identity.tenant_id,
            resource_type="secure-attachment", resource_id=str(attachment_id), field="upload-reference",
        )
        with connect(self.database_url) as connection:
            connection.execute(
                "UPDATE ai_secure_attachments SET upload_reference_envelope = %s, updated_at = CURRENT_TIMESTAMP WHERE attachment_id = %s AND tenant_id = %s AND user_id = %s",
                (envelope, attachment_id, identity.tenant_id, identity.user_id),
            )

    def _mark_provider_failure(self, identity: PersonalDomainIdentity, attachment_id: UUID, code: str) -> None:
        current = self.get(identity, attachment_id)
        self._transition(
            identity, attachment_id, uuid4(), current.revision, AttachmentState.PARTIAL,
            "UPLOAD_PROVIDER_UNAVAILABLE", self.fingerprints.value(
                tenant_id=identity.tenant_id, purpose="secure-attachment:provider-failure",
                payload={"attachmentId": str(attachment_id), "code": code},
            ), safe_error_code=safe_attachment_error(code),
        )

    @staticmethod
    def _event(connection: Any, identity: PersonalDomainIdentity, row: Any, command_id: UUID, event_type: str, previous: str | None, fingerprint: str, safe_error_code: str | None = None) -> None:
        connection.execute(
            """INSERT INTO ai_secure_attachment_events (
                   event_id, attachment_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint, safe_error_code)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (uuid4(), row["attachment_id"], identity.tenant_id, identity.user_id,
             identity.user_id, identity.correlation_id, command_id, event_type,
             previous, row["attachment_state"], row["revision"], fingerprint, safe_error_code),
        )


@lru_cache(maxsize=1)
def get_secure_attachment_store() -> SecureAttachmentStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise DwaionWorkflowUnavailable("Attachment storage is unavailable.")
    return SecureAttachmentStore(database_url)


_SELECT = "SELECT a.* FROM ai_secure_attachments a"
