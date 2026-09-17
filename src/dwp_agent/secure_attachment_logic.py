from __future__ import annotations

from .dwaion_workflow_contracts import (
    AttachmentCapabilities,
    AttachmentCitation,
    AttachmentStage,
    AttachmentStageKey,
    AttachmentStageState,
    AttachmentState,
    CreateAttachmentRequest,
    SecureAttachment,
)


def initial_attachment_stages(
    capabilities: AttachmentCapabilities, media_type: str
) -> list[AttachmentStage]:
    mapping = (
        (AttachmentStageKey.UPLOAD, capabilities.upload, True),
        (AttachmentStageKey.AV, capabilities.antivirus, True),
        (AttachmentStageKey.DLP, capabilities.dlp, True),
        (AttachmentStageKey.PARSER, capabilities.parser, True),
        (AttachmentStageKey.OCR, capabilities.ocr, media_type.startswith("image/")),
        (AttachmentStageKey.INDEX, capabilities.index, True),
    )
    return [
        AttachmentStage(
            key=key,
            state=(
                AttachmentStageState.PENDING
                if capability.available
                else AttachmentStageState.NOT_CONFIGURED
            )
            if required
            else AttachmentStageState.NOT_REQUIRED,
            safe_error_code=(
                None if capability.available or not required else capability.reason_code
            ),
            recovery_hint=(
                None if capability.available or not required else capability.recovery_hint
            ),
        )
        for key, capability, required in mapping
    ]


def derive_attachment_state(
    stages: list[AttachmentStage],
    citations: list[AttachmentCitation],
    media_type: str,
) -> AttachmentState:
    items = {stage.key: stage for stage in stages}
    values = {key: stage.state for key, stage in items.items()}
    required = {
        AttachmentStageKey.UPLOAD,
        AttachmentStageKey.AV,
        AttachmentStageKey.DLP,
        AttachmentStageKey.PARSER,
        AttachmentStageKey.INDEX,
    }
    if media_type.startswith("image/"):
        required.add(AttachmentStageKey.OCR)
    if any(values.get(key) == AttachmentStageState.BLOCKED for key in required):
        return AttachmentState.BLOCKED
    if any(values.get(key) == AttachmentStageState.FAILED for key in required):
        return AttachmentState.FAILED
    if any(values.get(key) == AttachmentStageState.NOT_CONFIGURED for key in required):
        return AttachmentState.PARTIAL
    verified = all(
        values.get(key) == AttachmentStageState.PASSED
        and (
            (
                key == AttachmentStageKey.UPLOAD
                and items[key].observed_at is not None
                and items[key].provider_code is not None
            )
            or (
                key != AttachmentStageKey.UPLOAD
                and items[key].observed_at is not None
                and items[key].provider_code is not None
                and items[key].provider_receipt_id is not None
                and items[key].result_digest is not None
            )
        )
        for key in required
    )
    if verified:
        return AttachmentState.READY if citations else AttachmentState.PARTIAL
    if all(values.get(key) == AttachmentStageState.PASSED for key in required):
        return AttachmentState.PARTIAL
    return AttachmentState.SCANNING


def matches_attachment_create(
    current: SecureAttachment, request: CreateAttachmentRequest
) -> bool:
    return (
        current.conversation_id == request.conversation_id
        and current.file_name == request.file_name
        and current.media_type == request.media_type.lower()
        and current.size_bytes == request.size_bytes
        and current.source_sha256 == request.source_sha256
    )


def safe_attachment_error(value: str) -> str:
    candidate = value.strip().upper().replace(" ", "_")
    return (
        candidate
        if candidate and all(ch.isalnum() or ch in "_.-" for ch in candidate)
        else "ATTACHMENT_PROVIDER_UNAVAILABLE"
    )
