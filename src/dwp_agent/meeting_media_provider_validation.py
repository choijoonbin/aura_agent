from __future__ import annotations

import hmac
import ipaddress
import re
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel

from .meeting_media_attestation import (
    MeetingMediaAttestationError,
    VerifiedMeetingMediaAttestation,
    verify_meeting_media_attestation,
)
from .meeting_media_contracts import (
    RecordingAccessTicketRequest,
    RecordingCapability,
    TranscriptRetentionCapability,
)
from .meeting_media_security import MeetingMediaPurpose


_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
_ACCESS_PREFIX = re.compile(r"^/[A-Za-z0-9._~/-]{1,200}/$")
_ACCESS_QUERY = re.compile(r"^(?:token|ticket)=[A-Za-z0-9._~-]{16,4096}$")


class MeetingMediaConfigurationError(RuntimeError):
    pass


class MeetingMediaUnavailable(RuntimeError):
    pass


class MeetingMediaProviderPolicy(Protocol):
    purpose: MeetingMediaPurpose
    enabled: bool
    base_url: str
    allowed_hosts: frozenset[str]
    api_token: str
    provider_code: str
    storage_provider_code: str
    processing_region: str
    policy_attestation: str
    attestation_public_key_base64: str
    attestation_key_id: str
    approved_policy_sha256: str
    access_allowed_hosts: frozenset[str]
    access_path_prefix: str
    access_ticket_ttl_seconds: int
    timeout_seconds: float
    maximum_response_bytes: int


def validate_provider_configuration(
    configuration: MeetingMediaProviderPolicy,
) -> VerifiedMeetingMediaAttestation:
    if not configuration.enabled:
        raise MeetingMediaConfigurationError("Meeting media broker is disabled.")
    host = _validated_origin(configuration.base_url, configuration.allowed_hosts)
    maximum = (
        1_000_000
        if configuration.purpose is MeetingMediaPurpose.RECORDING
        else 20_000_000
    )
    if (
        len(configuration.api_token) < 32
        or len(configuration.api_token) > 4_096
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in configuration.api_token
        )
        or not _SAFE_CODE.fullmatch(configuration.provider_code)
        or not _SAFE_CODE.fullmatch(configuration.storage_provider_code)
        or not _REGION.fullmatch(configuration.processing_region)
        or not 0.25 <= configuration.timeout_seconds <= 30.0
        or not 1_024 <= configuration.maximum_response_bytes <= maximum
    ):
        raise MeetingMediaConfigurationError("Meeting media broker is not configured.")
    if configuration.purpose is MeetingMediaPurpose.RECORDING:
        _validated_access_policy(configuration)
    try:
        return verify_meeting_media_attestation(
            compact_attestation=configuration.policy_attestation,
            public_key_base64=configuration.attestation_public_key_base64,
            expected_key_id=configuration.attestation_key_id,
            expected_purpose=configuration.purpose.value,
            expected_origin_host=host,
            expected_provider_code=configuration.provider_code,
            expected_storage_provider_code=configuration.storage_provider_code,
            expected_processing_region=configuration.processing_region,
            expected_policy_sha256=configuration.approved_policy_sha256,
        )
    except MeetingMediaAttestationError as error:
        raise MeetingMediaConfigurationError(
            "Meeting media broker attestation is not ready."
        ) from error


def recording_capability_matches(
    capability: RecordingCapability, attestation: VerifiedMeetingMediaAttestation
) -> bool:
    return (
        capability.available
        and capability.egress_available
        and capability.storage_available
        and capability.deletion_available
        and capability.crypto_shred_available
        and capability.orphan_cleanup_available
        and 30 <= capability.maximum_orphan_ttl_seconds <= 3_600
        and capability.customer_managed_storage
        and capability.provider_retention_disabled
        and capability.provider_code == attestation.provider_code
        and capability.processing_region == attestation.processing_region
        and capability.egress_available == attestation.egress_available
        and capability.storage_available == attestation.storage_available
        and capability.speech_to_text_available
        == attestation.speech_to_text_available
        and capability.deletion_available == attestation.deletion_available
        and capability.crypto_shred_available == attestation.crypto_shred_available
        and capability.orphan_cleanup_available == attestation.orphan_cleanup_available
        and capability.maximum_orphan_ttl_seconds
        == attestation.maximum_orphan_ttl_seconds
        and capability.customer_managed_storage
        == attestation.customer_managed_storage
        and capability.provider_retention_disabled
        == attestation.provider_retention_disabled
    )


def transcript_capability_matches(
    capability: TranscriptRetentionCapability,
    attestation: VerifiedMeetingMediaAttestation,
) -> bool:
    return (
        capability.available
        and capability.deletion_available
        and capability.crypto_shred_available
        and capability.customer_managed_storage
        and capability.provider_retention_disabled
        and capability.orphan_cleanup_available
        and 30 <= capability.maximum_orphan_ttl_seconds <= 3_600
        and capability.provider_code == attestation.provider_code
        and capability.storage_provider_code == attestation.storage_provider_code
        and capability.processing_region == attestation.processing_region
        and capability.deletion_available == attestation.deletion_available
        and capability.crypto_shred_available == attestation.crypto_shred_available
        and capability.orphan_cleanup_available == attestation.orphan_cleanup_available
        and capability.maximum_orphan_ttl_seconds
        == attestation.maximum_orphan_ttl_seconds
        and capability.customer_managed_storage
        == attestation.customer_managed_storage
        and capability.provider_retention_disabled
        == attestation.provider_retention_disabled
        and attestation.storage_available
        and attestation.speech_to_text_available
    )


def recording_unavailable() -> RecordingCapability:
    return RecordingCapability(
        available=False,
        egress_available=False,
        storage_available=False,
        speech_to_text_available=False,
        deletion_available=False,
        crypto_shred_available=False,
        orphan_cleanup_available=False,
        maximum_orphan_ttl_seconds=0,
        legacy_locator_deletion_available=False,
        customer_managed_storage=False,
        provider_retention_disabled=False,
        processing_region="none",
        provider_code="DISABLED",
    )


def transcript_unavailable() -> TranscriptRetentionCapability:
    return TranscriptRetentionCapability(
        available=False,
        deletion_available=False,
        crypto_shred_available=False,
        customer_managed_storage=False,
        provider_retention_disabled=False,
        orphan_cleanup_available=False,
        maximum_orphan_ttl_seconds=0,
        legacy_locator_deletion_available=False,
        provider_code="DISABLED",
        storage_provider_code="DISABLED",
        processing_region="none",
    )


def validate_deletion(response: BaseModel, request: BaseModel) -> None:
    if (
        getattr(response, "artifact_id") != getattr(request, "artifact_id")
        or getattr(response, "artifact_version")
        != getattr(request, "artifact_version")
        or not hmac.compare_digest(
            getattr(response, "deletion_binding_sha256"),
            getattr(request, "deletion_binding_sha256"),
        )
        or getattr(response, "deleted_at") > datetime.now(UTC) + timedelta(minutes=5)
    ):
        raise MeetingMediaUnavailable("Media deletion is unavailable.")


def validate_access_url(
    value: str,
    request: RecordingAccessTicketRequest,
    configuration: MeetingMediaProviderPolicy,
) -> None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    decoded = unquote(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in (None, 443)
        or host not in configuration.access_allowed_hosts
        or _unsafe_host(host)
        or not parsed.path.startswith(configuration.access_path_prefix)
        or len(parsed.path) <= len(configuration.access_path_prefix)
        or not _ACCESS_QUERY.fullmatch(parsed.query)
        or request.object_key in value
        or request.object_key in decoded
        or request.source_sha256 in value
        or request.source_sha256 in decoded
        or str(request.artifact_id) in value
        or str(request.artifact_id) in decoded
    ):
        raise MeetingMediaUnavailable("Recording access is unavailable.")


def _validated_origin(value: str, allowed_hosts: frozenset[str]) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise MeetingMediaConfigurationError("Media broker origin is invalid.") from error
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or port not in (None, 443)
        or host not in allowed_hosts
        or _unsafe_host(host)
    ):
        raise MeetingMediaConfigurationError("Media broker origin is invalid.")
    return host


def _validated_access_policy(configuration: MeetingMediaProviderPolicy) -> None:
    prefix = configuration.access_path_prefix
    if (
        not configuration.access_allowed_hosts
        or any(_unsafe_host(host) for host in configuration.access_allowed_hosts)
        or not _ACCESS_PREFIX.fullmatch(prefix)
        or "//" in prefix
        or "/../" in prefix
        or "/./" in prefix
        or not 30 <= configuration.access_ticket_ttl_seconds <= 600
    ):
        raise MeetingMediaConfigurationError("Recording access policy is invalid.")


def _unsafe_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
