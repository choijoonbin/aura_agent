from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifact_collaboration_contracts import (
    TeamArtifactCapabilities,
    TeamArtifactMember,
    TeamArtifactMemberRequest,
)
from .artifact_contracts import ArtifactSourceReference


class ArtifactCollaborationProviderUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ArtifactAclPreflightResult(_ProviderModel):
    artifactId: UUID
    teamId: UUID
    decisionRevision: int = Field(ge=1)
    members: list[TeamArtifactMember] = Field(min_length=1, max_length=100)
    allowedSources: list[ArtifactSourceReference] = Field(default_factory=list, max_length=20)
    excludedSources: list[ArtifactSourceReference] = Field(default_factory=list, max_length=20)
    evidenceSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expiresAt: datetime

    @field_validator("expiresAt")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Provider expiresAt must include an offset.")
        return value

    @model_validator(mode="after")
    def unique_partition(self) -> "ArtifactAclPreflightResult":
        if len({member.subject_id for member in self.members}) != len(self.members):
            raise ValueError("Provider members must be unique.")
        allowed = {
            (source.source_type.value, source.reference)
            for source in self.allowedSources
        }
        excluded = {
            (source.source_type.value, source.reference)
            for source in self.excludedSources
        }
        if (
            len(allowed) != len(self.allowedSources)
            or len(excluded) != len(self.excludedSources)
            or allowed & excluded
        ):
            raise ValueError("Provider sources must form a unique partition.")
        return self


@dataclass(frozen=True)
class ArtifactCollaborationProviderConfiguration:
    enabled: bool
    base_url: str
    service_token: str = field(repr=False)
    allowed_hosts: frozenset[str]
    timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "ArtifactCollaborationProviderConfiguration":
        return cls(
            enabled=_flag("DWP_ARTIFACT_COLLABORATION_ENABLED"),
            base_url=os.getenv("DWP_ARTIFACT_ACL_BROKER_BASE_URL", "").strip(),
            service_token=os.getenv(
                "DWP_ARTIFACT_ACL_BROKER_SERVICE_TOKEN", ""
            ).strip(),
            allowed_hosts=_csv("DWP_ARTIFACT_ACL_BROKER_ALLOWED_HOSTS"),
            timeout_seconds=_timeout(
                os.getenv("DWP_ARTIFACT_ACL_BROKER_TIMEOUT_SECONDS", "10")
            ),
        )

    def validate(self) -> None:
        if not self.enabled:
            raise ArtifactCollaborationProviderUnavailable(
                "ARTIFACT_COLLABORATION_NOT_CONFIGURED"
            )
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.hostname.lower() not in self.allowed_hosts
            or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"})
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not self.service_token
            or not 3 <= self.timeout_seconds <= 30
        ):
            raise ArtifactCollaborationProviderUnavailable(
                "ARTIFACT_COLLABORATION_NOT_CONFIGURED"
            )

    @property
    def configured(self) -> bool:
        try:
            self.validate()
        except ArtifactCollaborationProviderUnavailable:
            return False
        return True

    def capabilities(self) -> TeamArtifactCapabilities:
        available = self.configured
        return TeamArtifactCapabilities(
            team_workspace_available=available,
            acl_preflight_available=available,
            collaboration_available=available,
            conflict_resolution_available=available,
            internal_sharing_available=available,
            external_sharing_available=False,
            share_expiry_available=available,
            share_revocation_available=available,
            provider_state="AVAILABLE" if available else "NOT_CONFIGURED",
            recovery_hint=(
                None
                if available
                else "Configure and attest the artifact ACL broker."
            ),
        )


class ArtifactCollaborationProvider:
    def __init__(
        self,
        configuration: ArtifactCollaborationProviderConfiguration | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.configuration = (
            configuration or ArtifactCollaborationProviderConfiguration.from_environment()
        )
        self.transport = transport

    def capabilities(self) -> TeamArtifactCapabilities:
        return self.configuration.capabilities()

    def preflight(
        self,
        *,
        artifact_id: UUID,
        team_id: UUID,
        tenant_id: int,
        owner_user_id: str,
        artifact_revision: int,
        members: list[TeamArtifactMemberRequest],
        sources: list[ArtifactSourceReference],
        exclude_inaccessible_sources: bool,
        correlation_id: str,
    ) -> ArtifactAclPreflightResult:
        result = self._request(
            "/internal/v1/artifact-acl/preflights",
            {
                "artifactId": str(artifact_id),
                "teamId": str(team_id),
                "tenantId": tenant_id,
                "ownerUserId": owner_user_id,
                "artifactRevision": artifact_revision,
                "members": [member.model_dump(mode="json", by_alias=True) for member in members],
                "sources": [source.model_dump(mode="json", by_alias=True) for source in sources],
                "excludeInaccessibleSources": exclude_inaccessible_sources,
                "requireCurrentAuthorization": True,
            },
            correlation_id,
        )
        if result.artifactId != artifact_id or result.teamId != team_id:
            raise ArtifactCollaborationProviderUnavailable(
                "ARTIFACT_ACL_RESPONSE_MISMATCH"
            )
        expected_members = {(member.subject_id, member.role.value) for member in members}
        returned_members = {
            (member.subject_id, member.role.value) for member in result.members
        }
        if expected_members != returned_members or len(result.members) != len(
            expected_members
        ):
            raise ArtifactCollaborationProviderUnavailable(
                "ARTIFACT_ACL_SUBJECT_MISMATCH"
            )
        requested_sources = {
            (source.source_type.value, source.reference) for source in sources
        }
        returned_sources = {
            (source.source_type.value, source.reference)
            for source in [*result.allowedSources, *result.excludedSources]
        }
        if requested_sources != returned_sources:
            raise ArtifactCollaborationProviderUnavailable(
                "ARTIFACT_ACL_SOURCE_MISMATCH"
            )
        return result

    def _request(
        self,
        path: str,
        body: dict[str, object],
        correlation_id: str,
    ) -> ArtifactAclPreflightResult:
        self.configuration.validate()
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self.configuration.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    urljoin(
                        self.configuration.base_url.rstrip("/") + "/",
                        path.lstrip("/"),
                    ),
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.configuration.service_token}",
                        "X-Correlation-ID": correlation_id,
                        "Accept": "application/json",
                    },
                )
            if response.status_code != 200 or len(response.content) > 1_000_000:
                raise ArtifactCollaborationProviderUnavailable(
                    "ARTIFACT_ACL_PROVIDER_UNAVAILABLE"
                )
            return ArtifactAclPreflightResult.model_validate(response.json())
        except ArtifactCollaborationProviderUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise ArtifactCollaborationProviderUnavailable(
                "ARTIFACT_ACL_PROVIDER_UNAVAILABLE"
            ) from error


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def _csv(name: str) -> frozenset[str]:
    return frozenset(
        value.strip().lower()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    )


def _timeout(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0
