from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunStart:
    run_id: str
    tenant_id: str
    user_id: str
    request_id: str
    query_hash: str
    agent_key: str
    agent_revision: int
    risk_tier: str
    policy_outcome: str
    locale: str
    correlation_id: str
    audit_id: str | None = None


@dataclass(frozen=True)
class RunLease:
    run_id: str
    generation: int
