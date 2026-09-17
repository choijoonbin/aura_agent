from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from dwp_agent.attachment_action_contracts import (
    AttachmentRevisionBinding,
    CreateAttachmentAuditReportRequest,
    DetachAllAttachmentsRequest,
)
from dwp_agent.dwaion_workflow_contracts import DeleteAttachmentRequest
from dwp_agent.dwaion_workflow_contracts import AttachmentStageKey
from dwp_agent.attachment_stage_contracts import (
    AttachmentStageReceiptPayload,
    build_attachment_stage_receipt,
)
from dwp_agent.secure_attachment_provider import (
    AttachmentProviderConfiguration,
    AttachmentProviderUnavailable,
    SecureAttachmentProvider,
)


def test_attachment_governance_reasons_reject_whitespace() -> None:
    binding = AttachmentRevisionBinding(attachmentId=uuid4(), expectedRevision=1)
    candidates = (
        (DetachAllAttachmentsRequest, {
            "commandId": uuid4(), "idempotencyKey": uuid4(),
            "attachments": [binding], "reason": "     ",
        }),
        (CreateAttachmentAuditReportRequest, {
            "commandId": uuid4(), "idempotencyKey": uuid4(),
            "attachments": [binding], "reason": "     ",
        }),
        (DeleteAttachmentRequest, {
            "commandId": uuid4(), "expectedRevision": 1, "reason": "     ",
        }),
    )
    for contract, payload in candidates:
        with pytest.raises(ValidationError, match="reason"):
            contract.model_validate(payload)


def test_attachment_provider_rejects_blank_deletion_receipt() -> None:
    upload_reference = "upload-reference-1"
    provider = SecureAttachmentProvider(
        AttachmentProviderConfiguration(
            enabled=True,
            base_url="https://attachment-provider.test",
            service_token="test-service-token",
            allowed_hosts=frozenset({"attachment-provider.test"}),
            upload_allowed_hosts=frozenset({"upload.test"}),
            timeout_seconds=5,
            maximum_file_bytes=25_000_000,
            allowed_media_types=frozenset({"application/pdf"}),
            antivirus_available=True,
            dlp_available=True,
            parser_available=True,
            ocr_available=True,
            index_available=True,
            deletion_available=True,
        ),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={
                "uploadReference": upload_reference,
                "deleted": True,
                "providerReceiptId": "   ",
            })
        ),
    )

    with pytest.raises(AttachmentProviderUnavailable, match="ATTACHMENT_PROVIDER_UNAVAILABLE"):
        provider.delete(
            attachment_id=uuid4(),
            upload_reference=upload_reference,
            correlation_id="attachment-delete-test",
        )


def test_attachment_stage_provider_accepts_only_digest_and_target_bound_receipt() -> None:
    attachment_id = uuid4()
    upload_reference = "upload-reference-stage-1"
    source_sha256 = hashlib.sha256(b"stage source").hexdigest()
    payload = AttachmentStageReceiptPayload(
        attachmentId=attachment_id,
        uploadReference=upload_reference,
        sourceSha256=source_sha256,
        stage="AV",
        providerReceiptId="av-receipt-1",
        observedAt=datetime.now(UTC),
        verdict="PASSED",
        providerCode="CLEAN",
    )
    receipt = build_attachment_stage_receipt(payload)
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=receipt.model_dump(mode="json", by_alias=True))

    provider = SecureAttachmentProvider(
        _configuration(), transport=httpx.MockTransport(handle)
    )
    observed = provider.execute_stage(
        attachment_id=attachment_id,
        upload_reference=upload_reference,
        source_sha256=source_sha256,
        stage=AttachmentStageKey.AV,
        idempotency_key=uuid4(),
        correlation_id="attachment-stage-test",
    )

    assert observed == receipt
    assert captured["attachmentId"] == str(attachment_id)
    assert captured["uploadReference"] == upload_reference
    assert captured["sourceSha256"] == source_sha256
    assert captured["stage"] == "AV"


@pytest.mark.parametrize("mismatch", ["attachment", "upload", "source", "stage"])
def test_attachment_stage_provider_rejects_valid_but_misbound_receipt(
    mismatch: str,
) -> None:
    attachment_id = uuid4()
    upload_reference = "upload-reference-stage-2"
    source_sha256 = hashlib.sha256(b"stage source").hexdigest()
    values: dict[str, object] = {
        "attachmentId": uuid4() if mismatch == "attachment" else attachment_id,
        "uploadReference": (
            "different-upload-reference" if mismatch == "upload" else upload_reference
        ),
        "sourceSha256": (
            hashlib.sha256(b"different source").hexdigest()
            if mismatch == "source"
            else source_sha256
        ),
        "stage": "DLP" if mismatch == "stage" else "AV",
        "providerReceiptId": f"misbound-{mismatch}",
        "observedAt": datetime.now(UTC),
        "verdict": "PASSED",
        "providerCode": "CLEAN",
    }
    receipt = build_attachment_stage_receipt(
        AttachmentStageReceiptPayload.model_validate(values)
    )
    provider = SecureAttachmentProvider(
        _configuration(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json=receipt.model_dump(mode="json", by_alias=True)
            )
        ),
    )

    with pytest.raises(
        AttachmentProviderUnavailable,
        match="ATTACHMENT_STAGE_RECEIPT_BINDING_INVALID",
    ):
        provider.execute_stage(
            attachment_id=attachment_id,
            upload_reference=upload_reference,
            source_sha256=source_sha256,
            stage=AttachmentStageKey.AV,
            idempotency_key=uuid4(),
            correlation_id="attachment-stage-mismatch",
        )


def test_attachment_stage_provider_rejects_tampered_digest_and_blank_receipt() -> None:
    attachment_id = uuid4()
    source_sha256 = hashlib.sha256(b"stage source").hexdigest()
    payload = AttachmentStageReceiptPayload(
        attachmentId=attachment_id,
        uploadReference="upload-reference-stage-3",
        sourceSha256=source_sha256,
        stage="AV",
        providerReceiptId="av-receipt-3",
        observedAt=datetime.now(UTC),
        verdict="PASSED",
        providerCode="CLEAN",
    )
    response = {
        **payload.model_dump(mode="json", by_alias=True),
        "providerReceiptId": "   ",
        "resultDigest": "0" * 64,
    }
    provider = SecureAttachmentProvider(
        _configuration(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response)
        ),
    )

    with pytest.raises(
        AttachmentProviderUnavailable, match="ATTACHMENT_PROVIDER_UNAVAILABLE"
    ):
        provider.execute_stage(
            attachment_id=attachment_id,
            upload_reference="upload-reference-stage-3",
            source_sha256=source_sha256,
            stage=AttachmentStageKey.AV,
            idempotency_key=uuid4(),
            correlation_id="attachment-stage-tamper",
        )


def _configuration() -> AttachmentProviderConfiguration:
    return AttachmentProviderConfiguration(
        enabled=True,
        base_url="https://attachment-provider.test",
        service_token="test-service-token",
        allowed_hosts=frozenset({"attachment-provider.test"}),
        upload_allowed_hosts=frozenset({"upload.test"}),
        timeout_seconds=5,
        maximum_file_bytes=25_000_000,
        allowed_media_types=frozenset({"application/pdf", "text/plain"}),
        antivirus_available=True,
        dlp_available=True,
        parser_available=True,
        ocr_available=True,
        index_available=True,
        deletion_available=True,
    )
