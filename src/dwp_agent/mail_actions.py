from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from .contracts import ContractModel


class MailActionKind(StrEnum):
    DRAFT_REPLY = "DRAFT_REPLY"
    CREATE_CALENDAR_EVENT = "CREATE_CALENDAR_EVENT"
    CREATE_LEAVE_REQUEST = "CREATE_LEAVE_REQUEST"
    CREATE_TASK = "CREATE_TASK"
    ESCALATE_NOTIFICATION = "ESCALATE_NOTIFICATION"


class MailActionRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


MAIL_ACTION_CONTRACT_VERSION = 1


class MailActionEvidence(ContractModel):
    source_message_id: UUID
    observed_at: datetime
    rationale: str = Field(min_length=3, max_length=500)
    excerpt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class MailActionTarget(ContractModel):
    resource_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{2,127}$")
    permission_code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,63}$")
    route: str = Field(pattern=r"^/[A-Za-z0-9/_?&=.%:-]{1,999}$")
    expected_version: int | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class MailActionPolicy:
    resource_key: str
    permission_code: str
    route_prefix: str
    minimum_risk: MailActionRisk
    cross_application: bool
    required_payload_fields: frozenset[str]


MAIL_ACTION_POLICIES = MappingProxyType(
    {
        MailActionKind.DRAFT_REPLY: MailActionPolicy(
            "APP.MAIL",
            "CREATE",
            "/mail/",
            MailActionRisk.LOW,
            False,
            frozenset({"tone", "language", "requiresConfirmation"}),
        ),
        MailActionKind.CREATE_CALENDAR_EVENT: MailActionPolicy(
            "APP.CALENDAR",
            "CREATE",
            "/calendar/",
            MailActionRisk.MEDIUM,
            True,
            frozenset({"durationMinutes", "timeZone", "requiresConfirmation"}),
        ),
        MailActionKind.CREATE_LEAVE_REQUEST: MailActionPolicy(
            "APP.HCM",
            "VIEW",
            "/hr/",
            MailActionRisk.HIGH,
            True,
            frozenset({"durationDays", "requiresConfirmation"}),
        ),
        MailActionKind.CREATE_TASK: MailActionPolicy(
            "APP.WORK",
            "UPDATE",
            "/work",
            MailActionRisk.MEDIUM,
            True,
            frozenset({"priority", "requiresConfirmation"}),
        ),
        MailActionKind.ESCALATE_NOTIFICATION: MailActionPolicy(
            "APP.MAIL",
            "UPDATE",
            "/mail/",
            MailActionRisk.LOW,
            False,
            frozenset({"channel", "urgency", "requiresConfirmation"}),
        ),
    }
)


def mail_action_policy(action: MailActionKind) -> MailActionPolicy:
    policy = MAIL_ACTION_POLICIES.get(action)
    if policy is None:
        raise ValueError(f"No governed policy is registered for {action}.")
    return policy


class MailActionProposal(ContractModel):
    contract_version: Literal[1] = MAIL_ACTION_CONTRACT_VERSION
    proposal_id: UUID
    tenant_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    thread_id: UUID
    action: MailActionKind
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1_000)
    proposed_payload: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    evidence: list[MailActionEvidence] = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0, le=1)
    risk: MailActionRisk
    target: MailActionTarget
    expires_at: datetime
    human_confirmation_required: Literal[True] = True
    automatic_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_governed_target(self) -> "MailActionProposal":
        policy = mail_action_policy(self.action)
        if (self.target.resource_key, self.target.permission_code) != (
            policy.resource_key,
            policy.permission_code,
        ):
            raise ValueError("Mail action target does not match the governed resource policy.")
        if not self.target.route.startswith(policy.route_prefix):
            raise ValueError("Mail action route does not match the governed target application.")
        risk_order = {
            MailActionRisk.LOW: 0,
            MailActionRisk.MEDIUM: 1,
            MailActionRisk.HIGH: 2,
        }
        if risk_order[self.risk] < risk_order[policy.minimum_risk]:
            raise ValueError("Mail action risk is below the required policy floor.")
        if not policy.required_payload_fields.issubset(self.proposed_payload):
            raise ValueError("Mail action payload does not satisfy the governed contract.")
        if self.proposed_payload.get("requiresConfirmation") is not True:
            raise ValueError("Mail action payload must require explicit confirmation.")
        if self.expires_at <= max(item.observed_at for item in self.evidence):
            raise ValueError("Mail action must expire after its evidence was observed.")
        return self
