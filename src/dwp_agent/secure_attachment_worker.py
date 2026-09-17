from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from .attachment_stage_contracts import AttachmentWorkerObservation
from .dwaion_workflow_contracts import (
    AttachmentStageKey,
    AttachmentStageState,
    AttachmentState,
)
from .personal_domain_security import PersonalDomainIdentity
from .secure_attachment_provider import SecureAttachmentProvider
from .secure_attachment_pipeline import ATTACHMENT_PIPELINE_TOPIC
from .secure_attachment_store import SecureAttachmentStore
from .transactional_outbox import OutboxLease


TOPICS = (ATTACHMENT_PIPELINE_TOPIC,)
_PIPELINE_ORDER = (
    AttachmentStageKey.AV,
    AttachmentStageKey.DLP,
    AttachmentStageKey.PARSER,
    AttachmentStageKey.OCR,
    AttachmentStageKey.INDEX,
)


class PostgresSecureAttachmentWorker:
    """Executes durable attachment stages and persists only verified receipts."""

    def __init__(
        self,
        database_url: str,
        *,
        provider: SecureAttachmentProvider | None = None,
    ) -> None:
        self.store = SecureAttachmentStore(database_url, provider=provider)
        self.provider = self.store.provider

    def process(self, lease: OutboxLease) -> str:
        identity, attachment_id, upload_reference, source_sha256, media_type, required = (
            self._intent(lease)
        )
        binding = self.store.pipeline_binding(identity, attachment_id)
        attachment = binding.attachment
        expected_required = tuple(
            stage.key
            for stage in attachment.stages
            if stage.key != AttachmentStageKey.UPLOAD
            and stage.state != AttachmentStageState.NOT_REQUIRED
            and stage.state != AttachmentStageState.NOT_CONFIGURED
        )
        if (
            binding.upload_reference != upload_reference
            or attachment.source_sha256 != source_sha256
            or attachment.media_type != media_type
            or expected_required != required
        ):
            raise ValueError("The attachment pipeline intent binding is invalid.")
        if attachment.state in {
            AttachmentState.READY,
            AttachmentState.PARTIAL,
            AttachmentState.BLOCKED,
            AttachmentState.FAILED,
            AttachmentState.CANCELLED,
            AttachmentState.DELETION_PENDING,
            AttachmentState.DELETED,
        }:
            return attachment.state.value
        if attachment.state != AttachmentState.SCANNING:
            raise ValueError("The attachment is not ready for pipeline execution.")

        for stage_key in required:
            attachment = self.store.get(identity, attachment_id)
            stage = next(item for item in attachment.stages if item.key == stage_key)
            if stage.state in {
                AttachmentStageState.PASSED,
                AttachmentStageState.BLOCKED,
                AttachmentStageState.FAILED,
            }:
                if stage.state != AttachmentStageState.PASSED:
                    return attachment.state.value
                continue
            if stage.state != AttachmentStageState.PENDING:
                raise ValueError("The attachment pipeline stage is not executable.")
            receipt = self.provider.execute_stage(
                attachment_id=attachment_id,
                upload_reference=upload_reference,
                source_sha256=source_sha256,
                stage=stage_key,
                idempotency_key=uuid5(
                    NAMESPACE_URL,
                    (
                        "urn:dwp:secure-attachment-stage:"
                        f"{attachment_id}:{source_sha256}:{stage_key.value}"
                    ),
                ),
                correlation_id=identity.correlation_id,
            )
            attachment = self.store.observe(
                identity,
                attachment_id,
                AttachmentWorkerObservation(
                    command_id=uuid5(
                        NAMESPACE_URL,
                        f"urn:dwp:secure-attachment-observation:{lease.outbox_id}:{stage_key.value}",
                    ),
                    expected_revision=attachment.revision,
                    receipts=[receipt],
                ),
            )
            if attachment.state in {
                AttachmentState.BLOCKED,
                AttachmentState.FAILED,
            }:
                return attachment.state.value
        return self.store.get(identity, attachment_id).state.value

    def mark_retry_exhausted(self, lease: OutboxLease) -> None:
        identity, attachment_id, _, _, _, _ = self._intent(lease)
        self.store.mark_pipeline_retry_exhausted(
            identity,
            attachment_id,
            command_id=uuid5(
                NAMESPACE_URL,
                f"urn:dwp:secure-attachment-retry-exhausted:{lease.outbox_id}",
            ),
        )

    @staticmethod
    def _intent(
        lease: OutboxLease,
    ) -> tuple[
        PersonalDomainIdentity,
        UUID,
        str,
        str,
        str,
        tuple[AttachmentStageKey, ...],
    ]:
        if (
            lease.topic not in TOPICS
            or lease.aggregate_type != "SECURE_ATTACHMENT_PIPELINE"
        ):
            raise ValueError("The attachment pipeline outbox binding is invalid.")
        expected_fields = {
            "attachmentId",
            "uploadReference",
            "sourceSha256",
            "mediaType",
            "requiredStages",
            "attachmentRevision",
        }
        if set(lease.payload) != expected_fields:
            raise ValueError("The attachment pipeline intent schema is invalid.")
        try:
            attachment_id = UUID(str(lease.payload["attachmentId"]))
            upload_reference = str(lease.payload["uploadReference"])
            source_sha256 = str(lease.payload["sourceSha256"])
            media_type = str(lease.payload["mediaType"])
            attachment_revision = int(lease.payload["attachmentRevision"])
            raw_required = lease.payload["requiredStages"]
            if not isinstance(raw_required, list):
                raise ValueError("The attachment requiredStages value is invalid.")
            required = tuple(AttachmentStageKey(str(item)) for item in raw_required)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("The attachment pipeline intent is invalid.") from error
        if (
            lease.aggregate_id != str(attachment_id)
            or len(upload_reference) < 8
            or len(source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in source_sha256)
            or not media_type
            or attachment_revision < 1
            or required != tuple(key for key in _PIPELINE_ORDER if key in required)
            or len(set(required)) != len(required)
        ):
            raise ValueError("The attachment pipeline intent binding is invalid.")
        identity = PersonalDomainIdentity(
            tenant_id=lease.tenant_id,
            user_id=lease.user_id,
            correlation_id=f"secure-attachment:{lease.outbox_id}",
            auth_session_id=f"worker:{lease.outbox_id}",
            roles=frozenset({"WORKSPACE_MEMBER"}),
            permissions=frozenset(),
        )
        return (
            identity,
            attachment_id,
            upload_reference,
            source_sha256,
            media_type,
            required,
        )
