from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contract_model import ContractModel
from .dwaion_workflow_contracts import ResearchRun
from .personal_domain_security import PersonalDomainIdentity
from .workspace_authorization import WorkspaceRequestAuthorization


class ResearchRuntimeControls(ContractModel):
    excluded_source_keys: list[str] = Field(default_factory=list, max_length=100)
    reprobe_source_keys: list[str] = Field(default_factory=list, max_length=100)
    deadline_at: datetime | None = None
    extension_minutes: int = Field(default=0, ge=0, le=1_440)

    @field_validator("excluded_source_keys", "reprobe_source_keys")
    @classmethod
    def canonical_source_keys(cls, values: list[str]) -> list[str]:
        if any(
            not value
            or value != value.strip().upper()
            or len(value) > 128
            for value in values
        ):
            raise ValueError("Research runtime source keys are invalid.")
        if len(values) != len(set(values)):
            raise ValueError("Research runtime source keys must be unique.")
        return values

    @field_validator("deadline_at")
    @classmethod
    def aware_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Research runtime deadline must include a timezone.")
        return value

    @model_validator(mode="after")
    def source_sets_do_not_overlap(self) -> "ResearchRuntimeControls":
        if set(self.excluded_source_keys) & set(self.reprobe_source_keys):
            raise ValueError("Excluded and reprobed research sources cannot overlap.")
        return self


class ResearchExecutionAuthorization(ContractModel):
    correlation_id: str = Field(min_length=1, max_length=160)
    auth_session_id: str = Field(min_length=1, max_length=160)
    roles: list[str] = Field(default_factory=list, max_length=100)
    permissions: list[str] = Field(default_factory=list, max_length=500)
    cookie_header: str | None = Field(default=None, min_length=1, max_length=4_108)
    authorization_header: str | None = Field(
        default=None, min_length=8, max_length=8_192
    )
    session_family_id: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("roles", "permissions")
    @classmethod
    def canonical_authority_values(cls, values: list[str]) -> list[str]:
        if any(
            not value
            or value != value.strip().upper()
            or len(value) > 200
            for value in values
        ):
            raise ValueError("Research execution authority is invalid.")
        if values != sorted(set(values)):
            raise ValueError("Research execution authority must be sorted and unique.")
        return values

    @model_validator(mode="after")
    def delegated_authorization_present(self) -> "ResearchExecutionAuthorization":
        if not self.cookie_header and not self.authorization_header:
            raise ValueError("Research execution requires delegated workspace authorization.")
        return self

    @classmethod
    def capture(
        cls,
        identity: PersonalDomainIdentity,
        authorization: WorkspaceRequestAuthorization,
    ) -> "ResearchExecutionAuthorization":
        if not authorization.available:
            raise ValueError("Research execution authorization is unavailable.")
        return cls(
            correlation_id=identity.correlation_id,
            auth_session_id=identity.auth_session_id,
            roles=sorted(identity.roles),
            permissions=sorted(identity.permissions),
            cookie_header=authorization.cookie_header,
            authorization_header=authorization.authorization_header,
            session_family_id=authorization.session_family_id,
        )

    def identity(self, tenant_id: int, user_id: str) -> PersonalDomainIdentity:
        return PersonalDomainIdentity(
            tenant_id=tenant_id,
            user_id=user_id,
            correlation_id=self.correlation_id,
            auth_session_id=self.auth_session_id,
            roles=frozenset(self.roles),
            permissions=frozenset(self.permissions),
        )

    def workspace_authorization(self) -> WorkspaceRequestAuthorization:
        return WorkspaceRequestAuthorization(
            cookie_header=self.cookie_header,
            authorization_header=self.authorization_header,
            session_family_id=self.session_family_id,
        )


@dataclass(frozen=True)
class ResearchRunLease:
    run: ResearchRun
    identity: PersonalDomainIdentity
    authorization: ResearchExecutionAuthorization
    controls: ResearchRuntimeControls
    generation: int
    lease_token: UUID
    lease_expires_at: datetime

    def expired(self) -> bool:
        return self.lease_expires_at.astimezone(UTC) <= datetime.now(UTC)


class ResearchRunLeaseLost(RuntimeError):
    pass


class ResearchRunDeadlineExceeded(RuntimeError):
    pass
