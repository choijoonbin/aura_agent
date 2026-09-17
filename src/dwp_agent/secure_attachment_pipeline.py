from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .attachment_stage_contracts import (
    AttachmentStageProviderReceipt,
    AttachmentStageVerdict,
    AttachmentWorkerObservation,
)
from .dwaion_workflow_contracts import (
    AttachmentCitation,
    AttachmentStage,
    AttachmentStageKey,
    AttachmentStageState,
    AttachmentState,
    CompleteAttachmentUploadRequest,
    SecureAttachment,
)
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity
from .secure_attachment_logic import derive_attachment_state
from .secure_attachment_provider import AttachmentProviderUnavailable


ATTACHMENT_PIPELINE_TOPIC = "ai.secure-attachment.processing-requested.v1"
_SELECT = "SELECT a.* FROM ai_secure_attachments a"
_ORDER = {
    AttachmentStageKey.AV: 0,
    AttachmentStageKey.DLP: 1,
    AttachmentStageKey.PARSER: 2,
    AttachmentStageKey.OCR: 3,
    AttachmentStageKey.INDEX: 4,
}


@dataclass(frozen=True)
class AttachmentPipelineBinding:
    attachment: SecureAttachment
    upload_reference: str


class SecureAttachmentPipelineCommands:
    def complete_upload(
        self,
        identity: PersonalDomainIdentity,
        attachment_id: UUID,
        request: CompleteAttachmentUploadRequest,
    ) -> SecureAttachment:
        fingerprint = self._request_fingerprint(identity, "complete", request)
        binding = self.pipeline_binding(identity, attachment_id)
        current = binding.attachment
        if current.state not in {AttachmentState.UPLOADING, AttachmentState.PARTIAL}:
            return self._replay_or_conflict(
                identity, attachment_id, request.command_id, fingerprint
            )
        if request.upload_reference != binding.upload_reference:
            raise DwaionWorkflowConflict("The attachment upload reference has changed.")
        try:
            observed = self.provider.verify_upload(
                attachment_id=attachment_id,
                upload_reference=request.upload_reference,
                correlation_id=identity.correlation_id,
            )
        except AttachmentProviderUnavailable as error:
            self._transition(
                identity,
                attachment_id,
                request.command_id,
                request.expected_revision,
                AttachmentState.PARTIAL,
                "UPLOAD_VERIFY_PARTIAL",
                fingerprint,
                safe_error_code=str(error),
            )
            return self.get(identity, attachment_id)
        integrity_ok = (
            observed.uploadReference == request.upload_reference
            and observed.observedSizeBytes
            == current.size_bytes
            == request.observed_size_bytes
            and observed.observedSha256
            == current.source_sha256
            == request.observed_sha256
            and observed.observedMediaType.lower() == current.media_type.lower()
        )
        if not integrity_ok:
            self._transition(
                identity,
                attachment_id,
                request.command_id,
                request.expected_revision,
                AttachmentState.BLOCKED,
                "INTEGRITY_BLOCKED",
                fingerprint,
                safe_error_code="ATTACHMENT_INTEGRITY_MISMATCH",
            )
            return self.get(identity, attachment_id)
        stages = [
            stage.model_copy(
                update={
                    "state": AttachmentStageState.PASSED,
                    "provider_code": "UPLOAD_VERIFIED",
                    "observed_at": datetime.now(UTC),
                }
            )
            if stage.key == AttachmentStageKey.UPLOAD
            else stage
            for stage in current.stages
        ]
        next_state = derive_attachment_state(stages, [], current.media_type)
        pipeline_payload = None
        if next_state == AttachmentState.SCANNING:
            pipeline_payload = {
                "attachmentId": str(attachment_id),
                "uploadReference": binding.upload_reference,
                "sourceSha256": current.source_sha256,
                "mediaType": current.media_type,
                "requiredStages": [
                    stage.key.value
                    for stage in stages
                    if stage.key != AttachmentStageKey.UPLOAD
                    and stage.state == AttachmentStageState.PENDING
                ],
            }
        self._transition(
            identity,
            attachment_id,
            request.command_id,
            request.expected_revision,
            next_state,
            "UPLOAD_VERIFIED",
            fingerprint,
            stages=stages,
            pipeline_payload=pipeline_payload,
        )
        return self.get(identity, attachment_id)

    def observe(
        self,
        identity: PersonalDomainIdentity,
        attachment_id: UUID,
        request: AttachmentWorkerObservation,
    ) -> SecureAttachment:
        fingerprint = self._request_fingerprint(identity, "observe", request)
        binding = self.pipeline_binding(identity, attachment_id)
        current = binding.attachment
        if current.revision != request.expected_revision:
            return self._replay_or_conflict(
                identity, attachment_id, request.command_id, fingerprint
            )
        if current.state == AttachmentState.DELETION_PENDING:
            raise DwaionWorkflowConflict(
                "Attachment deletion can only be confirmed by the configured storage provider."
            )
        if current.state not in {AttachmentState.SCANNING, AttachmentState.PARTIAL}:
            raise DwaionWorkflowConflict(
                "The attachment is not awaiting pipeline evidence."
            )
        stages, citations = self._apply_stage_receipts(binding, request.receipts)
        state = derive_attachment_state(stages, citations, current.media_type)
        self._transition(
            identity,
            attachment_id,
            request.command_id,
            request.expected_revision,
            state,
            "WORKER_OBSERVED",
            fingerprint,
            stages=stages,
            citations=citations,
        )
        return self.get(identity, attachment_id)

    def pipeline_binding(
        self, identity: PersonalDomainIdentity, attachment_id: UUID
    ) -> AttachmentPipelineBinding:
        capabilities = self.capabilities(identity)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    _SELECT
                    + " WHERE a.attachment_id = %s AND a.tenant_id = %s AND a.user_id = %s",
                    (attachment_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if row is None:
                    raise DwaionWorkflowNotFound(
                        "The secure attachment is unavailable."
                    )
                if row["upload_reference_envelope"] is None:
                    raise DwaionWorkflowConflict(
                        "The attachment upload reference is unavailable."
                    )
                reference = self.codec.decrypt_json(
                    row["upload_reference_envelope"],
                    tenant_id=row["tenant_id"],
                    resource_type="secure-attachment",
                    resource_id=str(row["attachment_id"]),
                    field="upload-reference",
                ).get("uploadReference")
                if not isinstance(reference, str) or not reference.strip():
                    raise DwaionWorkflowConflict(
                        "The attachment upload reference is unavailable."
                    )
                return AttachmentPipelineBinding(
                    attachment=self._record(row, capabilities),
                    upload_reference=reference,
                )
        except (DwaionWorkflowNotFound, DwaionWorkflowConflict):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable(
                "Attachment pipeline binding is unavailable."
            ) from error

    def mark_pipeline_retry_exhausted(
        self,
        identity: PersonalDomainIdentity,
        attachment_id: UUID,
        *,
        command_id: UUID,
    ) -> SecureAttachment:
        current = self.get(identity, attachment_id)
        if current.state in {
            AttachmentState.READY,
            AttachmentState.BLOCKED,
            AttachmentState.FAILED,
            AttachmentState.CANCELLED,
            AttachmentState.DELETION_PENDING,
            AttachmentState.DELETED,
        }:
            return current
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="secure-attachment:pipeline-retry-exhausted",
            payload={
                "attachmentId": str(attachment_id),
                "commandId": str(command_id),
            },
        )
        self._transition(
            identity,
            attachment_id,
            command_id,
            current.revision,
            AttachmentState.FAILED,
            "PIPELINE_RETRY_EXHAUSTED",
            fingerprint,
            safe_error_code="ATTACHMENT_PIPELINE_RETRY_EXHAUSTED",
        )
        return self.get(identity, attachment_id)

    def _apply_stage_receipts(
        self,
        binding: AttachmentPipelineBinding,
        receipts: list[AttachmentStageProviderReceipt],
    ) -> tuple[list[AttachmentStage], list[AttachmentCitation]]:
        current = binding.attachment
        stages = {stage.key: stage for stage in current.stages}
        citations = {item.citation_id: item for item in current.citations}
        receipt_ids = {
            stage.provider_receipt_id
            for stage in current.stages
            if stage.provider_receipt_id is not None
        }
        result_digests = {
            stage.result_digest
            for stage in current.stages
            if stage.result_digest is not None
        }
        now = datetime.now(UTC)
        ordered_receipts = sorted(receipts, key=lambda item: _ORDER[item.stage])
        for position, receipt in enumerate(ordered_receipts):
            self._validate_receipt_target(binding, receipt, now)
            stage = stages.get(receipt.stage)
            if stage is None or stage.state not in {
                AttachmentStageState.PENDING,
                AttachmentStageState.RUNNING,
            }:
                raise DwaionWorkflowConflict(
                    "The attachment stage is not awaiting provider evidence."
                )
            if any(
                stages[key].state != AttachmentStageState.PASSED
                for key in self._prerequisites(receipt.stage, stages)
            ):
                raise DwaionWorkflowConflict(
                    "The attachment stage prerequisites are not verified."
                )
            if (
                receipt.provider_receipt_id in receipt_ids
                or receipt.result_digest in result_digests
            ):
                raise DwaionWorkflowConflict(
                    "The attachment stage receipt was already bound to another stage."
                )
            for citation in receipt.citations:
                previous = citations.get(citation.citation_id)
                if previous is not None and previous != citation:
                    raise DwaionWorkflowConflict(
                        "The attachment citation ID is already bound to different evidence."
                    )
                citations[citation.citation_id] = citation
            stages[receipt.stage] = AttachmentStage(
                key=receipt.stage,
                state=AttachmentStageState(receipt.verdict.value),
                provider_code=receipt.provider_code,
                provider_receipt_id=receipt.provider_receipt_id,
                result_digest=receipt.result_digest,
                observed_at=receipt.observed_at,
                safe_error_code=receipt.safe_error_code,
                recovery_hint=receipt.recovery_hint,
            )
            receipt_ids.add(receipt.provider_receipt_id)
            result_digests.add(receipt.result_digest)
            if receipt.verdict != AttachmentStageVerdict.PASSED:
                if position != len(ordered_receipts) - 1:
                    raise DwaionWorkflowConflict(
                        "A blocked or failed attachment stage must end the observation."
                    )
                break
        return [stages[key] for key in AttachmentStageKey], list(citations.values())

    @staticmethod
    def _validate_receipt_target(
        binding: AttachmentPipelineBinding,
        receipt: AttachmentStageProviderReceipt,
        now: datetime,
    ) -> None:
        current = binding.attachment
        if (
            receipt.attachment_id != current.attachment_id
            or receipt.upload_reference != binding.upload_reference
            or receipt.source_sha256 != current.source_sha256
        ):
            raise DwaionWorkflowConflict(
                "The attachment stage receipt target binding is invalid."
            )
        if (
            receipt.observed_at < current.created_at - timedelta(minutes=5)
            or receipt.observed_at > now + timedelta(minutes=5)
        ):
            raise DwaionWorkflowConflict(
                "The attachment stage receipt observation time is invalid."
            )

    @staticmethod
    def _prerequisites(
        stage: AttachmentStageKey,
        stages: dict[AttachmentStageKey, AttachmentStage],
    ) -> tuple[AttachmentStageKey, ...]:
        fixed = {
            AttachmentStageKey.AV: (AttachmentStageKey.UPLOAD,),
            AttachmentStageKey.DLP: (
                AttachmentStageKey.UPLOAD,
                AttachmentStageKey.AV,
            ),
            AttachmentStageKey.PARSER: (
                AttachmentStageKey.UPLOAD,
                AttachmentStageKey.AV,
                AttachmentStageKey.DLP,
            ),
            AttachmentStageKey.OCR: (
                AttachmentStageKey.UPLOAD,
                AttachmentStageKey.AV,
                AttachmentStageKey.DLP,
                AttachmentStageKey.PARSER,
            ),
        }
        if stage in fixed:
            return fixed[stage]
        return tuple(
            key
            for key in (
                AttachmentStageKey.UPLOAD,
                AttachmentStageKey.AV,
                AttachmentStageKey.DLP,
                AttachmentStageKey.PARSER,
                AttachmentStageKey.OCR,
            )
            if stages[key].state != AttachmentStageState.NOT_REQUIRED
        )
