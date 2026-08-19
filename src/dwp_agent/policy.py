from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import AskPolicyDecision, PolicyOutcome, RiskTier


PRIVILEGED_PATTERNS = (
    r"\b(?:salary|payroll|compensation|bonus|bank account|routing number|social security|ssn|tax id)\b",
    r"(?:급여|월급|연봉|보상정보|성과급|보너스|계좌번호|주민(?:등록)?번호|민감정보)",
)
ADMIN_MUTATION_PATTERNS = (
    r"\b(?:grant|revoke|delete|disable|terminate|approve|reject|publish|provision)\b",
    r"\b(?:assign|remove)\s+(?:a\s+)?(?:role|permission|access)\b",
    r"(?:권한을?\s*(?:부여|회수|삭제)|계정을?\s*(?:삭제|비활성)|승인해|반려해|게시해)",
)
PERSONAL_SCOPE_PATTERNS = (
    r"\b(?:my|mine|me|i am|i'm)\b",
    r"(?:내\s|나의|제가|나는|내가)",
)


@dataclass(frozen=True)
class AskIdentity:
    tenant_id: str
    user_id: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    correlation_id: str


def evaluate_ask_policy(
    query: str,
    identity: AskIdentity,
    *,
    agent_key: str = "DWP_ASSISTANT",
) -> AskPolicyDecision:
    normalized = " ".join(query.strip().split())
    permission_set = {permission.upper() for permission in identity.permissions}

    if "APP.ASK:VIEW" not in permission_set:
        return AskPolicyDecision(
            outcome=PolicyOutcome.DENY,
            risk_tier=RiskTier.L1,
            code="ASK_PERMISSION_REQUIRED",
            explanation="DWAI·ON access is not present in the verified session scope.",
            model_allowed=False,
        )

    if agent_key.strip().upper() == "DWP_APPROVAL_EXPERT" and "APP.APPROVALS:VIEW" not in permission_set:
        return AskPolicyDecision(
            outcome=PolicyOutcome.DENY,
            risk_tier=RiskTier.L1,
            code="APPROVAL_EXPERT_PERMISSION_REQUIRED",
            explanation="Approval application access is not present in the verified session scope.",
            model_allowed=False,
        )

    if contains_privileged_data(normalized):
        return AskPolicyDecision(
            outcome=PolicyOutcome.HANDOFF,
            risk_tier=RiskTier.L3,
            code="PRIVILEGED_DATA_HANDOFF",
            explanation="The request may involve privileged workforce or financial data.",
            model_allowed=False,
        )

    if _matches(ADMIN_MUTATION_PATTERNS, normalized):
        return AskPolicyDecision(
            outcome=PolicyOutcome.HANDOFF,
            risk_tier=RiskTier.L2,
            code="MUTATION_REQUIRES_GOVERNED_WORKFLOW",
            explanation="The request describes a change that requires an approved application workflow.",
            model_allowed=False,
        )

    personal = _matches(PERSONAL_SCOPE_PATTERNS, normalized)
    return AskPolicyDecision(
        outcome=PolicyOutcome.ALLOW,
        risk_tier=RiskTier.L1 if personal else RiskTier.L0,
        code="READ_ONLY_GROUNDED_ANSWER",
        explanation="A read-only answer may be generated from sources in the verified user scope.",
        model_allowed=True,
    )


def _matches(patterns: tuple[str, ...], value: str) -> bool:
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def contains_privileged_data(value: str) -> bool:
    normalized = " ".join(value.strip().split())
    return _matches(PRIVILEGED_PATTERNS, normalized)
