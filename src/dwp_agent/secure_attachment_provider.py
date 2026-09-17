from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .dwaion_workflow_contracts import (
    AttachmentCapabilities,
    AttachmentStageKey,
    AttachmentUploadTicket,
    WorkflowCapability,
)
from .attachment_stage_contracts import AttachmentStageProviderReceipt


class AttachmentProviderUnavailable(RuntimeError):
    pass


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _UploadResponse(_ProviderModel):
    uploadReference: str = Field(min_length=8, max_length=1_000)
    uploadUrl: str = Field(min_length=8, max_length=8_192)
    expiresAt: datetime


class _VerifyResponse(_ProviderModel):
    uploadReference: str = Field(min_length=8, max_length=1_000)
    observedSizeBytes: int = Field(ge=1, le=104_857_600)
    observedSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observedMediaType: str = Field(min_length=3, max_length=120)


class _DeleteResponse(_ProviderModel):
    uploadReference: str = Field(min_length=8, max_length=1_000)
    deleted: bool
    providerReceiptId: str = Field(min_length=1, max_length=240)

    @field_validator("providerReceiptId")
    @classmethod
    def valid_provider_receipt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Attachment deletion requires a provider receipt.")
        return normalized


@dataclass(frozen=True)
class AttachmentProviderConfiguration:
    enabled: bool
    base_url: str
    service_token: str = field(repr=False)
    allowed_hosts: frozenset[str]
    upload_allowed_hosts: frozenset[str]
    timeout_seconds: float
    maximum_file_bytes: int
    allowed_media_types: frozenset[str]
    antivirus_available: bool
    dlp_available: bool
    parser_available: bool
    ocr_available: bool
    index_available: bool
    deletion_available: bool

    @classmethod
    def from_environment(cls) -> "AttachmentProviderConfiguration":
        return cls(
            enabled=_flag("DWP_ATTACHMENT_BROKER_ENABLED"),
            base_url=os.getenv("DWP_ATTACHMENT_BROKER_BASE_URL", "").strip(),
            service_token=os.getenv("DWP_ATTACHMENT_BROKER_SERVICE_TOKEN", "").strip(),
            allowed_hosts=_csv("DWP_ATTACHMENT_BROKER_ALLOWED_HOSTS"),
            upload_allowed_hosts=_csv("DWP_ATTACHMENT_UPLOAD_ALLOWED_HOSTS"),
            timeout_seconds=float(os.getenv("DWP_ATTACHMENT_BROKER_TIMEOUT_SECONDS", "10")),
            maximum_file_bytes=int(
                os.getenv("DWP_ATTACHMENT_MAXIMUM_FILE_BYTES", "26214400")
            ),
            allowed_media_types=_csv(
                "DWP_ATTACHMENT_ALLOWED_MEDIA_TYPES",
                default=(
                    "application/pdf",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "image/png",
                    "image/jpeg",
                    "text/plain",
                    "text/csv",
                ),
            ),
            antivirus_available=_flag("DWP_ATTACHMENT_AV_AVAILABLE"),
            dlp_available=_flag("DWP_ATTACHMENT_DLP_AVAILABLE"),
            parser_available=_flag("DWP_ATTACHMENT_PARSER_AVAILABLE"),
            ocr_available=_flag("DWP_ATTACHMENT_OCR_AVAILABLE"),
            index_available=_flag("DWP_ATTACHMENT_INDEX_AVAILABLE"),
            deletion_available=_flag("DWP_ATTACHMENT_DELETE_AVAILABLE"),
        )

    def validate(self) -> None:
        if not self.enabled:
            raise AttachmentProviderUnavailable("ATTACHMENT_PROVIDER_NOT_CONFIGURED")
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.hostname.lower() not in self.allowed_hosts
            or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"})
            or not self.service_token
            or not (1 <= self.timeout_seconds <= 30)
            or not (1 <= self.maximum_file_bytes <= 104_857_600)
        ):
            raise AttachmentProviderUnavailable("ATTACHMENT_PROVIDER_NOT_CONFIGURED")

    def capabilities(self) -> AttachmentCapabilities:
        configured = True
        reason = None
        try:
            self.validate()
        except AttachmentProviderUnavailable:
            configured = False
            reason = "ATTACHMENT_PROVIDER_NOT_CONFIGURED"
        return AttachmentCapabilities(
            upload=_capability(configured, reason),
            antivirus=_capability(configured and self.antivirus_available, "AV_NOT_CONFIGURED"),
            dlp=_capability(configured and self.dlp_available, "DLP_NOT_CONFIGURED"),
            parser=_capability(configured and self.parser_available, "PARSER_NOT_CONFIGURED"),
            ocr=_capability(configured and self.ocr_available, "OCR_NOT_CONFIGURED"),
            index=_capability(configured and self.index_available, "INDEX_NOT_CONFIGURED"),
            deletion=_capability(configured and self.deletion_available, "DELETE_NOT_CONFIGURED"),
            detach_all=_capability(False, "DETACH_ALL_NOT_IMPLEMENTED"),
            inspection_log=_capability(True, None),
            masking_history=_capability(False, "MASKING_HISTORY_NOT_CONFIGURED"),
            ocr_viewer=_capability(configured and self.ocr_available, "OCR_NOT_CONFIGURED"),
            signed_audit_report=_capability(False, "SIGNED_AUDIT_REPORT_NOT_CONFIGURED"),
            maximum_file_bytes=self.maximum_file_bytes,
            allowed_media_types=sorted(self.allowed_media_types),
        )


class SecureAttachmentProvider:
    def __init__(
        self,
        configuration: AttachmentProviderConfiguration | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.configuration = configuration or AttachmentProviderConfiguration.from_environment()
        self.transport = transport

    def capabilities(self) -> AttachmentCapabilities:
        return self.configuration.capabilities()

    def create_upload(
        self,
        *,
        attachment_id: UUID,
        tenant_id: int,
        user_id: str,
        media_type: str,
        size_bytes: int,
        source_sha256: str,
        correlation_id: str,
    ) -> AttachmentUploadTicket:
        self._allow(media_type, size_bytes)
        response = self._request(
            "POST",
            "/internal/v1/attachments/uploads",
            {
                "attachmentId": str(attachment_id),
                "tenantId": tenant_id,
                "userId": user_id,
                "mediaType": media_type,
                "sizeBytes": size_bytes,
                "sourceSha256": source_sha256,
            },
            correlation_id,
            _UploadResponse,
        )
        self._validate_upload_url(response.uploadUrl)
        return AttachmentUploadTicket(
            upload_url=response.uploadUrl,
            upload_reference=response.uploadReference,
            expires_at=response.expiresAt,
        )

    def verify_upload(
        self,
        *,
        attachment_id: UUID,
        upload_reference: str,
        correlation_id: str,
    ) -> _VerifyResponse:
        return self._request(
            "POST",
            "/internal/v1/attachments/uploads/verify",
            {"attachmentId": str(attachment_id), "uploadReference": upload_reference},
            correlation_id,
            _VerifyResponse,
        )

    def delete(
        self,
        *,
        attachment_id: UUID,
        upload_reference: str,
        correlation_id: str,
    ) -> _DeleteResponse:
        if not self.configuration.deletion_available:
            raise AttachmentProviderUnavailable("ATTACHMENT_DELETE_NOT_CONFIGURED")
        response = self._request(
            "POST",
            "/internal/v1/attachments/delete",
            {"attachmentId": str(attachment_id), "uploadReference": upload_reference},
            correlation_id,
            _DeleteResponse,
        )
        if not response.deleted or response.uploadReference != upload_reference:
            raise AttachmentProviderUnavailable("ATTACHMENT_DELETE_UNVERIFIED")
        return response

    def execute_stage(
        self,
        *,
        attachment_id: UUID,
        upload_reference: str,
        source_sha256: str,
        stage: AttachmentStageKey,
        idempotency_key: UUID,
        correlation_id: str,
    ) -> AttachmentStageProviderReceipt:
        if stage == AttachmentStageKey.UPLOAD or not self._stage_available(stage):
            raise AttachmentProviderUnavailable(
                f"ATTACHMENT_{stage.value}_NOT_CONFIGURED"
            )
        response = self._request(
            "POST",
            "/internal/v1/attachments/stages/execute",
            {
                "attachmentId": str(attachment_id),
                "uploadReference": upload_reference,
                "sourceSha256": source_sha256,
                "stage": stage.value,
                "idempotencyKey": str(idempotency_key),
            },
            correlation_id,
            AttachmentStageProviderReceipt,
        )
        if (
            response.attachment_id != attachment_id
            or response.upload_reference != upload_reference
            or response.source_sha256 != source_sha256
            or response.stage != stage
        ):
            raise AttachmentProviderUnavailable(
                "ATTACHMENT_STAGE_RECEIPT_BINDING_INVALID"
            )
        return response

    def _allow(self, media_type: str, size_bytes: int) -> None:
        self.configuration.validate()
        if media_type not in self.configuration.allowed_media_types:
            raise AttachmentProviderUnavailable("ATTACHMENT_MEDIA_TYPE_BLOCKED")
        if size_bytes > self.configuration.maximum_file_bytes:
            raise AttachmentProviderUnavailable("ATTACHMENT_SIZE_LIMIT_EXCEEDED")

    def _stage_available(self, stage: AttachmentStageKey) -> bool:
        return {
            AttachmentStageKey.AV: self.configuration.antivirus_available,
            AttachmentStageKey.DLP: self.configuration.dlp_available,
            AttachmentStageKey.PARSER: self.configuration.parser_available,
            AttachmentStageKey.OCR: self.configuration.ocr_available,
            AttachmentStageKey.INDEX: self.configuration.index_available,
        }.get(stage, False)

    def _request(self, method: str, path: str, body: dict[str, object], correlation_id: str, model):
        self.configuration.validate()
        try:
            with httpx.Client(
                timeout=self.configuration.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = client.request(
                    method,
                    urljoin(self.configuration.base_url.rstrip("/") + "/", path.lstrip("/")),
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.configuration.service_token}",
                        "X-Correlation-ID": correlation_id,
                        "Accept": "application/json",
                    },
                )
            if response.status_code != 200 or len(response.content) > 1_000_000:
                raise AttachmentProviderUnavailable("ATTACHMENT_PROVIDER_UNAVAILABLE")
            return model.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as error:
            raise AttachmentProviderUnavailable("ATTACHMENT_PROVIDER_UNAVAILABLE") from error

    def _validate_upload_url(self, value: str) -> None:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.lower() not in self.configuration.upload_allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise AttachmentProviderUnavailable("ATTACHMENT_UPLOAD_URL_REJECTED")


def _capability(available: bool, reason: str | None) -> WorkflowCapability:
    return WorkflowCapability(
        available=available,
        configured=available,
        reason_code=None if available else reason,
        recovery_hint=None if available else "Configure and attest the required attachment provider.",
    )


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def _csv(name: str, default: tuple[str, ...] = ()) -> frozenset[str]:
    raw = os.getenv(name, "")
    values = raw.split(",") if raw.strip() else default
    return frozenset(value.strip().lower() for value in values if value.strip())
