from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_contracts import ArtifactDraftContent, ExportFormat
from .artifact_export_renderer import render_artifact_export
from .attachment_action_contracts import (
    AttachmentAuditReportReceipt,
    AttachmentDetachReceipt,
    CreateAttachmentAuditReportRequest,
    DetachAllAttachmentsRequest,
    DetachedAttachment,
)
from .attachment_audit_signing import AttachmentAuditSigningUnavailable
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import advisory_lock
from .personal_domain_security import PersonalDomainIdentity


class SecureAttachmentGovernedActions:
    database_url: str
    codec: Any
    fingerprints: Any
    audit_signer: Any

    def detach_all(
        self,
        identity: PersonalDomainIdentity,
        conversation_id: UUID,
        request: DetachAllAttachmentsRequest,
    ) -> AttachmentDetachReceipt:
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="secure-attachment:detach-all",
            payload={
                "conversationId": str(conversation_id),
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "attachment-detach-all",
                    identity.tenant_id,
                    identity.user_id,
                    request.command_id,
                )
                replay = self._detach_replay(connection, identity, request, fingerprint)
                if replay is not None:
                    return replay
                rows = connection.execute(
                    """SELECT * FROM ai_secure_attachments
                        WHERE tenant_id = %s AND user_id = %s AND conversation_id = %s
                          AND attachment_state <> 'DELETED'
                        ORDER BY attachment_id
                        FOR UPDATE""",
                    (identity.tenant_id, identity.user_id, conversation_id),
                ).fetchall()
                expected = {
                    item.attachment_id: item.expected_revision for item in request.attachments
                }
                if not rows or {row["attachment_id"] for row in rows} != set(expected):
                    raise DwaionWorkflowConflict(
                        "The conversation attachment selection has changed."
                    )
                if any(int(row["revision"]) != expected[row["attachment_id"]] for row in rows):
                    raise DwaionWorkflowConflict("An attachment revision has changed.")

                detached: list[DetachedAttachment] = []
                for row in rows:
                    updated = connection.execute(
                        """UPDATE ai_secure_attachments
                              SET conversation_id = NULL, revision = revision + 1,
                                  updated_at = CURRENT_TIMESTAMP
                            WHERE attachment_id = %s
                        RETURNING *""",
                        (row["attachment_id"],),
                    ).fetchone()
                    event_command = uuid5(
                        NAMESPACE_URL,
                        f"urn:dwp:attachment-detach:{request.command_id}:{row['attachment_id']}",
                    )
                    self._event(
                        connection,
                        identity,
                        updated,
                        event_command,
                        "DETACHED_FROM_CONVERSATION",
                        row["attachment_state"],
                        fingerprint,
                    )
                    detached.append(
                        DetachedAttachment(
                            attachment_id=updated["attachment_id"],
                            revision=updated["revision"],
                        )
                    )
                detached_at = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                receipt_id = uuid4()
                receipt_payload = {
                    "receiptId": str(receipt_id),
                    "commandId": str(request.command_id),
                    "conversationId": str(conversation_id),
                    "detachedAttachments": [
                        item.model_dump(mode="json", by_alias=True) for item in detached
                    ],
                    "detachedAt": detached_at.isoformat(),
                }
                integrity = self.fingerprints.value(
                    tenant_id=identity.tenant_id,
                    purpose="secure-attachment:detach-receipt",
                    payload=receipt_payload,
                )
                receipt = AttachmentDetachReceipt.model_validate(
                    {**receipt_payload, "integrityFingerprint": integrity}
                )
                envelope = self.codec.encrypt_json(
                    receipt.model_dump(mode="json", by_alias=True),
                    tenant_id=identity.tenant_id,
                    resource_type="attachment-detach-receipt",
                    resource_id=str(receipt_id),
                    field="receipt",
                )
                connection.execute(
                    """INSERT INTO ai_attachment_detach_commands (
                           receipt_id, tenant_id, user_id, conversation_id,
                           command_id, idempotency_key, request_fingerprint,
                           receipt_envelope, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        receipt_id,
                        identity.tenant_id,
                        identity.user_id,
                        conversation_id,
                        request.command_id,
                        request.idempotency_key,
                        fingerprint,
                        envelope,
                        detached_at,
                    ),
                )
                return receipt
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable(
                "Attachment detach storage is unavailable."
            ) from error

    def create_audit_report(
        self,
        identity: PersonalDomainIdentity,
        conversation_id: UUID,
        request: CreateAttachmentAuditReportRequest,
    ) -> AttachmentAuditReportReceipt:
        capability = self.audit_signer.capability(identity.tenant_id)
        if not capability.available or not capability.configured:
            raise DwaionWorkflowUnavailable(
                capability.reason_code or "ATTACHMENT_AUDIT_SIGNING_NOT_CONFIGURED"
            )
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="secure-attachment:audit-report",
            payload={
                "conversationId": str(conversation_id),
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "attachment-audit-report",
                    identity.tenant_id,
                    identity.user_id,
                    request.command_id,
                )
                replay = self._audit_replay(connection, identity, request, fingerprint)
                if replay is not None:
                    return replay
                rows = connection.execute(
                    """SELECT * FROM ai_secure_attachments
                        WHERE tenant_id = %s AND user_id = %s AND conversation_id = %s
                          AND attachment_state <> 'DELETED'
                        ORDER BY attachment_id
                        FOR UPDATE""",
                    (identity.tenant_id, identity.user_id, conversation_id),
                ).fetchall()
                expected = {
                    item.attachment_id: item.expected_revision for item in request.attachments
                }
                if not rows or {row["attachment_id"] for row in rows} != set(expected):
                    raise DwaionWorkflowConflict(
                        "The conversation attachment selection has changed."
                    )
                if any(int(row["revision"]) != expected[row["attachment_id"]] for row in rows):
                    raise DwaionWorkflowConflict("An attachment revision has changed.")
                attachments = [
                    self._record(row, self.capabilities(identity)) for row in rows
                ]
                event_rows = connection.execute(
                        """SELECT attachment_id, COUNT(*) AS event_count
                             FROM ai_secure_attachment_events
                            WHERE tenant_id = %s AND user_id = %s
                              AND attachment_id = ANY(%s)
                            GROUP BY attachment_id""",
                        (
                            identity.tenant_id,
                            identity.user_id,
                            [item.attachment_id for item in attachments],
                        ),
                    ).fetchall()
                event_counts = {
                    row["attachment_id"]: int(row["event_count"])
                    for row in event_rows
                }
                markdown = _audit_markdown(conversation_id, attachments, event_counts)
                rendered = render_artifact_export(
                    ArtifactDraftContent(
                        title="DWAI.ON Secure Attachment Verification Report",
                        body=markdown,
                    ),
                    ExportFormat.PDF,
                )
                content = rendered.content
                content_sha256 = hashlib.sha256(content).hexdigest()
                signature = self.audit_signer.sign(identity.tenant_id, content)
                report_id = uuid4()
                created_at = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                receipt = AttachmentAuditReportReceipt(
                    report_id=report_id,
                    command_id=request.command_id,
                    conversation_id=conversation_id,
                    attachment_ids=[item.attachment_id for item in attachments],
                    content_sha256=content_sha256,
                    signature_algorithm=signature.algorithm,
                    signature=signature.signature,
                    signing_key_fingerprint=signature.key_fingerprint,
                    download_path=f"/v1/attachments/audit-reports/{report_id}/download",
                    created_at=created_at,
                )
                report_envelope = self.codec.encrypt_json(
                    {"contentBase64": base64.b64encode(content).decode()},
                    tenant_id=identity.tenant_id,
                    resource_type="attachment-audit-report",
                    resource_id=str(report_id),
                    field="content",
                )
                receipt_envelope = self.codec.encrypt_json(
                    receipt.model_dump(mode="json", by_alias=True),
                    tenant_id=identity.tenant_id,
                    resource_type="attachment-audit-report",
                    resource_id=str(report_id),
                    field="receipt",
                )
                connection.execute(
                    """INSERT INTO ai_attachment_audit_reports (
                           report_id, tenant_id, user_id, conversation_id,
                           command_id, idempotency_key, request_fingerprint,
                           report_envelope, receipt_envelope, content_sha256,
                           signature, signing_key_fingerprint, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        report_id,
                        identity.tenant_id,
                        identity.user_id,
                        conversation_id,
                        request.command_id,
                        request.idempotency_key,
                        fingerprint,
                        report_envelope,
                        receipt_envelope,
                        content_sha256,
                        signature.signature,
                        signature.key_fingerprint,
                        created_at,
                    ),
                )
                self._audit_event(
                    connection,
                    identity,
                    report_id,
                    request.command_id,
                    "GENERATED",
                    content_sha256,
                )
                return receipt
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound, DwaionWorkflowUnavailable):
            raise
        except AttachmentAuditSigningUnavailable as error:
            raise DwaionWorkflowUnavailable(str(error)) from error
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable(
                "Attachment audit report storage is unavailable."
            ) from error

    def download_audit_report(
        self, identity: PersonalDomainIdentity, report_id: UUID
    ) -> bytes:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """SELECT * FROM ai_attachment_audit_reports
                        WHERE report_id = %s AND tenant_id = %s AND user_id = %s""",
                    (report_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if row is None:
                    raise DwaionWorkflowNotFound(
                        "The attachment audit report is unavailable."
                    )
                payload = self.codec.decrypt_json(
                    row["report_envelope"],
                    tenant_id=identity.tenant_id,
                    resource_type="attachment-audit-report",
                    resource_id=str(report_id),
                    field="content",
                )
                content = base64.b64decode(str(payload["contentBase64"]), validate=True)
                if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
                    raise DwaionWorkflowUnavailable(
                        "Attachment audit report integrity verification failed."
                    )
                signature = self.audit_signer.sign(identity.tenant_id, content)
                if (
                    signature.key_fingerprint != row["signing_key_fingerprint"]
                    or not hmac.compare_digest(signature.signature, row["signature"])
                ):
                    raise DwaionWorkflowUnavailable(
                        "Attachment audit report signature verification failed."
                    )
                self._audit_event(
                    connection,
                    identity,
                    report_id,
                    uuid4(),
                    "DOWNLOADED",
                    row["content_sha256"],
                )
                return content
        except (DwaionWorkflowNotFound, DwaionWorkflowUnavailable):
            raise
        except AttachmentAuditSigningUnavailable as error:
            raise DwaionWorkflowUnavailable(str(error)) from error
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable(
                "Attachment audit report download is unavailable."
            ) from error

    def _detach_replay(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        request: DetachAllAttachmentsRequest,
        fingerprint: str,
    ) -> AttachmentDetachReceipt | None:
        row = connection.execute(
            """SELECT * FROM ai_attachment_detach_commands
                WHERE tenant_id = %s AND user_id = %s
                  AND (command_id = %s OR idempotency_key = %s)""",
            (
                identity.tenant_id,
                identity.user_id,
                request.command_id,
                request.idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        if row["request_fingerprint"] != fingerprint:
            raise DwaionWorkflowConflict(
                "The attachment detach command identity is already in use."
            )
        payload = self.codec.decrypt_json(
            row["receipt_envelope"],
            tenant_id=identity.tenant_id,
            resource_type="attachment-detach-receipt",
            resource_id=str(row["receipt_id"]),
            field="receipt",
        )
        return AttachmentDetachReceipt.model_validate(payload)

    def _audit_replay(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        request: CreateAttachmentAuditReportRequest,
        fingerprint: str,
    ) -> AttachmentAuditReportReceipt | None:
        row = connection.execute(
            """SELECT * FROM ai_attachment_audit_reports
                WHERE tenant_id = %s AND user_id = %s
                  AND (command_id = %s OR idempotency_key = %s)""",
            (
                identity.tenant_id,
                identity.user_id,
                request.command_id,
                request.idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        if row["request_fingerprint"] != fingerprint:
            raise DwaionWorkflowConflict(
                "The attachment audit report command identity is already in use."
            )
        payload = self.codec.decrypt_json(
            row["receipt_envelope"],
            tenant_id=identity.tenant_id,
            resource_type="attachment-audit-report",
            resource_id=str(row["report_id"]),
            field="receipt",
        )
        return AttachmentAuditReportReceipt.model_validate(payload)

    @staticmethod
    def _audit_event(
        connection: Any,
        identity: PersonalDomainIdentity,
        report_id: UUID,
        command_id: UUID,
        event_type: str,
        content_sha256: str,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_attachment_audit_report_events (
                   event_id, report_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, content_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                report_id,
                identity.tenant_id,
                identity.user_id,
                identity.user_id,
                identity.correlation_id,
                command_id,
                event_type,
                content_sha256,
            ),
        )


def _audit_markdown(
    conversation_id: UUID,
    attachments: list[Any],
    event_counts: dict[UUID, int],
) -> str:
    lines = [
        f"Conversation: `{conversation_id}`",
        "",
        "## Verified attachment evidence",
        "",
    ]
    for attachment in attachments:
        passed = [stage.key.value for stage in attachment.stages if stage.state.value == "PASSED"]
        lines.extend(
            [
                f"### {attachment.file_name}",
                f"- Attachment ID: `{attachment.attachment_id}`",
                f"- Revision: {attachment.revision}",
                f"- State: {attachment.state.value}",
                f"- Source SHA-256: `{attachment.source_sha256}`",
                f"- Passed stages: {', '.join(passed) if passed else 'none'}",
                f"- Evidence events: {event_counts.get(attachment.attachment_id, 0)}",
                f"- Citations: {len(attachment.citations)}",
                "",
            ]
        )
    lines.extend(
        [
            "## Governance statement",
            "",
            "This report is generated from tenant- and user-bound server evidence. "
            "The content digest and tenant signing evidence are recorded in the immutable audit receipt.",
        ]
    )
    return "\n".join(lines)
