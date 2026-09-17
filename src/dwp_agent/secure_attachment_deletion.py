from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg import connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import (
    AttachmentState,
    DeleteAttachmentRequest,
    SecureAttachment,
)
from .dwaion_workflow_errors import DwaionWorkflowConflict, DwaionWorkflowNotFound
from .governed_domain_core import advisory_lock
from .personal_domain_security import PersonalDomainIdentity
from .secure_attachment_provider import AttachmentProviderUnavailable


_SELECT = "SELECT a.* FROM ai_secure_attachments a"


class SecureAttachmentDeletionCommands:
    database_url: str
    provider: Any
    codec: Any
    fingerprints: Any

    def delete(
        self,
        identity: PersonalDomainIdentity,
        attachment_id: UUID,
        request: DeleteAttachmentRequest,
    ) -> SecureAttachment:
        fingerprint = self._request_fingerprint(identity, "delete", request)
        current = self.get(identity, attachment_id)
        if current.state == AttachmentState.DELETED:
            return self._replay_or_conflict(
                identity, attachment_id, request.command_id, fingerprint
            )
        self._transition(
            identity,
            attachment_id,
            request.command_id,
            request.expected_revision,
            AttachmentState.DELETION_PENDING,
            "DELETE_REQUESTED",
            fingerprint,
        )
        attempt, upload_reference = self._begin_deletion_attempt(
            identity, attachment_id
        )
        if attempt is None:
            return self.get(identity, attachment_id)
        if upload_reference is None:
            self._record_deletion_failure(
                identity,
                attachment_id,
                attempt,
                "ATTACHMENT_UPLOAD_REFERENCE_UNAVAILABLE",
            )
            return self.get(identity, attachment_id)
        try:
            receipt = self.provider.delete(
                attachment_id=attachment_id,
                upload_reference=upload_reference,
                correlation_id=identity.correlation_id,
            )
        except AttachmentProviderUnavailable as error:
            self._record_deletion_failure(
                identity,
                attachment_id,
                attempt,
                _safe_attachment_error(str(error)),
            )
            return self.get(identity, attachment_id)
        self._confirm_provider_deletion(
            identity,
            attachment_id,
            upload_reference=upload_reference,
            provider_receipt_id=receipt.providerReceiptId,
        )
        return self.get(identity, attachment_id)

    def _begin_deletion_attempt(
        self, identity: PersonalDomainIdentity, attachment_id: UUID
    ) -> tuple[int | None, str | None]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            advisory_lock(
                connection,
                "attachment-provider-delete",
                identity.tenant_id,
                identity.user_id,
                attachment_id,
            )
            row = connection.execute(
                _SELECT
                + " WHERE a.attachment_id = %s AND a.tenant_id = %s AND a.user_id = %s FOR UPDATE",
                (attachment_id, identity.tenant_id, identity.user_id),
            ).fetchone()
            if row is None:
                raise DwaionWorkflowNotFound("The secure attachment is unavailable.")
            if row["attachment_state"] == AttachmentState.DELETED.value:
                return None, None
            if row["attachment_state"] != AttachmentState.DELETION_PENDING.value:
                raise DwaionWorkflowConflict("The attachment is not pending deletion.")
            attempt = int(row["deletion_attempt_count"]) + 1
            connection.execute(
                """UPDATE ai_secure_attachments
                      SET deletion_attempt_count = %s,
                          deletion_last_attempt_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE attachment_id = %s AND tenant_id = %s AND user_id = %s""",
                (attempt, attachment_id, identity.tenant_id, identity.user_id),
            )
            envelope = row["upload_reference_envelope"]
        if not envelope:
            return attempt, None
        payload = self.codec.decrypt_json(
            envelope,
            tenant_id=identity.tenant_id,
            resource_type="secure-attachment",
            resource_id=str(attachment_id),
            field="upload-reference",
        )
        reference = payload.get("uploadReference")
        if (
            not isinstance(reference, str)
            or not 8 <= len(reference) <= 1_000
            or "://" in reference
            or "?" in reference
            or "#" in reference
        ):
            return attempt, None
        return attempt, reference

    def _record_deletion_failure(
        self,
        identity: PersonalDomainIdentity,
        attachment_id: UUID,
        attempt: int,
        safe_error_code: str,
    ) -> None:
        safe_code = _safe_attachment_error(safe_error_code)
        command_id = uuid5(
            NAMESPACE_URL,
            f"urn:dwp:attachment-delete-failure:{identity.tenant_id}:{identity.user_id}:{attachment_id}:{attempt}",
        )
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="secure-attachment:delete-provider-failure",
            payload={
                "attachmentId": str(attachment_id),
                "attempt": attempt,
                "safeErrorCode": safe_code,
            },
        )
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                _SELECT
                + " WHERE a.attachment_id = %s AND a.tenant_id = %s AND a.user_id = %s FOR UPDATE",
                (attachment_id, identity.tenant_id, identity.user_id),
            ).fetchone()
            if row is None:
                raise DwaionWorkflowNotFound("The secure attachment is unavailable.")
            if row["attachment_state"] == AttachmentState.DELETED.value:
                return
            if row["attachment_state"] != AttachmentState.DELETION_PENDING.value:
                raise DwaionWorkflowConflict("The attachment is not pending deletion.")
            connection.execute(
                """UPDATE ai_secure_attachments
                      SET deletion_last_error_code = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE attachment_id = %s AND tenant_id = %s AND user_id = %s""",
                (safe_code, attachment_id, identity.tenant_id, identity.user_id),
            )
            self._event(
                connection,
                identity,
                row,
                command_id,
                "DELETE_RETRY_PENDING",
                row["attachment_state"],
                fingerprint,
                safe_code,
            )

    def _confirm_provider_deletion(
        self,
        identity: PersonalDomainIdentity,
        attachment_id: UUID,
        *,
        upload_reference: str,
        provider_receipt_id: str,
    ) -> None:
        command_id = uuid5(
            NAMESPACE_URL,
            f"urn:dwp:attachment-delete-confirmed:{identity.tenant_id}:{identity.user_id}:{attachment_id}",
        )
        receipt = {
            "providerReceiptId": provider_receipt_id,
            "uploadReference": upload_reference,
        }
        envelope = self.codec.encrypt_json(
            receipt,
            tenant_id=identity.tenant_id,
            resource_type="secure-attachment",
            resource_id=str(attachment_id),
            field="deletion-receipt",
        )
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="secure-attachment:delete-provider-confirmed",
            payload={
                "attachmentId": str(attachment_id),
                "providerReceiptId": provider_receipt_id,
            },
        )
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                _SELECT
                + " WHERE a.attachment_id = %s AND a.tenant_id = %s AND a.user_id = %s FOR UPDATE",
                (attachment_id, identity.tenant_id, identity.user_id),
            ).fetchone()
            if row is None:
                raise DwaionWorkflowNotFound("The secure attachment is unavailable.")
            if row["attachment_state"] == AttachmentState.DELETED.value:
                return
            if row["attachment_state"] != AttachmentState.DELETION_PENDING.value:
                raise DwaionWorkflowConflict("The attachment is not pending deletion.")
            updated = connection.execute(
                """UPDATE ai_secure_attachments
                      SET attachment_state = 'DELETED', revision = revision + 1,
                          upload_reference_envelope = NULL,
                          deletion_receipt_envelope = %s,
                          deletion_last_error_code = NULL,
                          deleted_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE attachment_id = %s AND tenant_id = %s AND user_id = %s
                RETURNING *""",
                (envelope, attachment_id, identity.tenant_id, identity.user_id),
            ).fetchone()
            self._event(
                connection,
                identity,
                updated,
                command_id,
                "DELETE_PROVIDER_CONFIRMED",
                row["attachment_state"],
                fingerprint,
            )


def _safe_attachment_error(value: str) -> str:
    candidate = value.strip().upper().replace(" ", "_")
    return (
        candidate
        if candidate and all(ch.isalnum() or ch in "_.-" for ch in candidate)
        else "ATTACHMENT_PROVIDER_UNAVAILABLE"
    )
