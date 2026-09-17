from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException, Request, status

from .home_widget_identity import (
    ASSERTION_HEADER,
    DEFAULT_KEY_ID,
    HomeDelegatedIdentityError,
    HomeIdentityReplayUnavailable,
    home_identity_replay_store,
    verify_home_delegated_identity,
)


AUTHORITY_REVISION_HEADER = "X-DWP-Current-Decision-Revision"
_AUTHORITY = re.compile(r"^[A-Za-z0-9._:@+-]{1,160}$")
_REVISION = re.compile(r"^[A-Za-z0-9._:@+-]{1,200}$")
_LOCALE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,79}$")
_PROTECTED_HEADERS = (
    AUTHORITY_REVISION_HEADER,
    "X-DWP-Tenant-ID",
    "X-DWP-User-ID",
    "X-DWP-Person-Public-ID",
    "X-DWP-Permissions",
    "X-DWP-Roles",
    "X-DWP-Group-Refs",
    "X-DWP-Current-Revalidate-At",
    "X-DWP-Home-Deadline-At",
    "X-Correlation-ID",
    "X-DWP-Identity-Plane",
    ASSERTION_HEADER,
)


@dataclass(frozen=True)
class HomeWidgetRecipient:
    tenant_id: int
    user_id: int
    permissions: frozenset[str]
    roles: frozenset[str]
    groups: frozenset[str]
    authority_decision_revision: str
    authority_revalidate_at: datetime
    deadline_at: datetime
    locale: str

    def current(self) -> None:
        now = datetime.now(timezone.utc)
        if now >= self.authority_revalidate_at:
            _error(
                status.HTTP_401_UNAUTHORIZED,
                "HOME_PROVIDER_AUTHORITY_EXPIRED",
                "Current recipient authority evidence is missing or expired.",
            )
        if now >= self.deadline_at:
            _error(
                status.HTTP_401_UNAUTHORIZED,
                "HOME_PROVIDER_DEADLINE_EXPIRED",
                "Current recipient authority evidence is missing or expired.",
            )


async def authorize_home_widget_request(request: Request) -> HomeWidgetRecipient:
    signing_secret = os.getenv(
        "DWP_DWAION_HOME_IDENTITY_SIGNING_SECRET", ""
    ).strip()
    if len(signing_secret) < 32:
        _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "HOME_PROVIDER_DELEGATED_IDENTITY_NOT_CONFIGURED",
            "The dedicated DWAI-ON Home delegated identity verifier is not configured.",
        )
    key_id = os.getenv(
        "DWP_DWAION_HOME_IDENTITY_KEY_ID", DEFAULT_KEY_ID
    ).strip()
    if not _KEY_ID.fullmatch(key_id):
        _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "HOME_PROVIDER_DELEGATED_IDENTITY_NOT_CONFIGURED",
            "The dedicated DWAI-ON Home delegated identity verifier is not configured.",
        )
    if any(len(request.headers.getlist(name)) > 1 for name in _PROTECTED_HEADERS):
        _error(
            status.HTTP_400_BAD_REQUEST,
            "HOME_PROVIDER_HEADER_INVALID",
            "Security-sensitive Home provider headers must occur exactly once.",
        )
    required = (
        AUTHORITY_REVISION_HEADER,
        "X-DWP-Tenant-ID",
        "X-DWP-User-ID",
        "X-DWP-Current-Revalidate-At",
        "X-DWP-Home-Deadline-At",
        "X-Correlation-ID",
        "X-DWP-Identity-Plane",
        ASSERTION_HEADER,
    )
    if any(len(request.headers.getlist(name)) != 1 for name in required):
        _error(
            status.HTTP_401_UNAUTHORIZED,
            "HOME_PROVIDER_AUTHORITY_EVIDENCE_MISSING",
            "Required Home provider identity and recipient evidence is missing.",
        )
    if any(
        request.headers.get(name)
        for name in (
            "Authorization",
            "Cookie",
            "X-DWP-Service-Identity",
            "X-DWP-Service-Token",
            "X-DWP-Support-Session-ID",
            "X-DWP-Provider-Tenant-ID",
            "X-DWP-Actor-Tenant-ID",
        )
    ):
        _error(
            status.HTTP_403_FORBIDDEN,
            "HOME_PROVIDER_AMBIENT_AUTHORITY_REJECTED",
            "Ambient browser or support authority is not accepted by a Home provider.",
        )
    if request.headers["X-DWP-Identity-Plane"] != "TENANT":
        _error(
            status.HTTP_403_FORBIDDEN,
            "HOME_PROVIDER_IDENTITY_PLANE_REJECTED",
            "Only a tenant recipient identity can read personal DWAI-ON artifacts.",
        )
    try:
        body = await request.body()
        if not body or len(body) > 262_144:
            _error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "HOME_PROVIDER_BODY_OUT_OF_BOUNDS",
                "The Home provider request body is outside the signed contract bound.",
            )
        verify_home_delegated_identity(
            assertion=request.headers[ASSERTION_HEADER],
            secret=signing_secret,
            method=request.method,
            path=request.url.path,
            body=body,
            headers=request.headers,
            replay_store=home_identity_replay_store(),
            key_id=key_id,
        )
    except HomeIdentityReplayUnavailable:
        _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "HOME_PROVIDER_REPLAY_PROTECTION_UNAVAILABLE",
            "DWAI-ON Home replay protection is unavailable.",
        )
    except HomeDelegatedIdentityError:
        _error(
            status.HTTP_401_UNAUTHORIZED,
            "HOME_PROVIDER_DELEGATED_IDENTITY_INVALID",
            "A valid signed recipient identity is required.",
        )

    tenant_id = _positive_int(request.headers["X-DWP-Tenant-ID"])
    user_id = _positive_int(request.headers["X-DWP-User-ID"])
    person_public_id = request.headers.get("X-DWP-Person-Public-ID")
    if person_public_id:
        try:
            UUID(person_public_id)
        except ValueError:
            _error(
                status.HTTP_401_UNAUTHORIZED,
                "HOME_PROVIDER_RECIPIENT_INVALID",
                "The recipient person binding is invalid.",
            )
    revision = request.headers[AUTHORITY_REVISION_HEADER].strip()
    if not _REVISION.fullmatch(revision):
        _error(
            status.HTTP_401_UNAUTHORIZED,
            "HOME_PROVIDER_AUTHORITY_REVISION_INVALID",
            "A current authority decision revision is required.",
        )
    now = datetime.now(timezone.utc)
    revalidate_at = _future(
        request.headers["X-DWP-Current-Revalidate-At"],
        now,
        "HOME_PROVIDER_AUTHORITY_EXPIRED",
    )
    deadline_at = _future(
        request.headers["X-DWP-Home-Deadline-At"],
        now,
        "HOME_PROVIDER_DEADLINE_EXPIRED",
    )
    if deadline_at > now + timedelta(seconds=30):
        _error(
            status.HTTP_400_BAD_REQUEST,
            "HOME_PROVIDER_DEADLINE_UNBOUNDED",
            "The provider deadline exceeds the transport budget.",
        )
    locale = request.headers.get("Accept-Language", "ko-KR").strip()
    if not _LOCALE.fullmatch(locale):
        locale = "ko-KR"
    return HomeWidgetRecipient(
        tenant_id=tenant_id,
        user_id=user_id,
        permissions=_authority_set(request.headers.get("X-DWP-Permissions", "")),
        roles=_authority_set(request.headers.get("X-DWP-Roles", "")),
        groups=_authority_set(request.headers.get("X-DWP-Group-Refs", "")),
        authority_decision_revision=revision,
        authority_revalidate_at=revalidate_at,
        deadline_at=deadline_at,
        locale=locale,
    )


def _authority_set(raw: str) -> frozenset[str]:
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if len(values) > 256 or any(not _AUTHORITY.fullmatch(value) for value in values):
        _error(
            status.HTTP_400_BAD_REQUEST,
            "HOME_PROVIDER_AUTHORITY_SET_INVALID",
            "Recipient authority sets must be bounded canonical identifiers.",
        )
    if len(values) != len(set(values)):
        _error(
            status.HTTP_400_BAD_REQUEST,
            "HOME_PROVIDER_AUTHORITY_SET_INVALID",
            "Recipient authority sets must not contain duplicates.",
        )
    return frozenset(values)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        parsed = 0
    if parsed <= 0 or str(parsed) != value:
        _error(
            status.HTTP_401_UNAUTHORIZED,
            "HOME_PROVIDER_RECIPIENT_INVALID",
            "A positive canonical recipient tenant and user are required.",
        )
    return parsed


def _future(raw: str, now: datetime, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = now
    if parsed.tzinfo is None or parsed <= now:
        _error(
            status.HTTP_401_UNAUTHORIZED,
            code,
            "Current recipient authority evidence is missing or expired.",
        )
    return parsed.astimezone(timezone.utc)


def _error(http_status: int, reason_code: str, message: str) -> None:
    raise HTTPException(
        status_code=http_status,
        detail={"reasonCode": reason_code, "message": message},
    )
