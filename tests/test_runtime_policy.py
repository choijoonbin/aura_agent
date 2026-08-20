from datetime import datetime, timezone

import pytest

from dwp_agent.contracts import AskRequest, CitationSourceType, PolicyOutcome
from dwp_agent.governance_contracts import (
    ConnectionState,
    DataClassification,
    DataSourcePolicy,
    SafetyPolicy,
    SourceAccessMode,
)
from dwp_agent.policy import AskIdentity
from dwp_agent.runtime_policy import (
    SourcePolicyBlocked,
    SourceScopeLimitExceeded,
    resolve_runtime_safety_controls,
)


class FakeGovernanceStore:
    def __init__(
        self,
        *,
        blocked: set[CitationSourceType] | None = None,
        max_sources: int = 7,
    ) -> None:
        blocked = blocked or set()
        now = datetime.now(timezone.utc)
        self.sources = [
            DataSourcePolicy(
                source_key=source,
                display_name=source.value,
                description="Verified source",
                provider_type="DWP_TEST",
                classification=DataClassification.INTERNAL,
                access_mode=(
                    SourceAccessMode.BLOCKED
                    if source in blocked
                    else SourceAccessMode.SOURCE_PERMISSIONS
                ),
                enabled=source not in blocked,
                connection_state=(
                    ConnectionState.BLOCKED
                    if source in blocked
                    else ConnectionState.CONNECTED
                ),
                policy_version=1,
                updated_at=now,
            )
            for source in CitationSourceType
        ]
        self.safety = SafetyPolicy(
            prompt_injection_outcome=PolicyOutcome.DENY,
            privileged_data_outcome=PolicyOutcome.DENY,
            mutation_outcome=PolicyOutcome.HANDOFF,
            require_citations=True,
            public_web_enabled=False,
            max_source_scopes=max_sources,
            max_tool_calls=3,
            policy_version=1,
            updated_at=now,
        )

    def source_policies(self, *, tenant_id: str, actor_user_id: str):
        assert (tenant_id, actor_user_id) == ("1", "7")
        return self.sources

    def safety_policy(self, *, tenant_id: str, actor_user_id: str):
        assert (tenant_id, actor_user_id) == ("1", "7")
        return self.safety


IDENTITY = AskIdentity(
    tenant_id="1",
    user_id="7",
    roles=("WORKSPACE_MEMBER",),
    permissions=("APP.ASK:VIEW",),
    correlation_id="corr-1",
)


def test_runtime_policy_applies_tenant_safety_outcomes() -> None:
    request = AskRequest(
        request_id="runtime-policy",
        query="Show my current work",
        source_scopes=[CitationSourceType.WORK_ITEM],
    )

    controls = resolve_runtime_safety_controls(
        request,
        IDENTITY,
        governance_store=FakeGovernanceStore(),
    )

    assert controls.privileged_data_outcome == PolicyOutcome.DENY
    assert controls.mutation_outcome == PolicyOutcome.HANDOFF


def test_runtime_policy_rejects_blocked_source_and_scope_overflow() -> None:
    blocked_request = AskRequest(
        request_id="blocked-source",
        query="Show my mail",
        source_scopes=[CitationSourceType.MAIL],
    )
    with pytest.raises(SourcePolicyBlocked):
        resolve_runtime_safety_controls(
            blocked_request,
            IDENTITY,
            governance_store=FakeGovernanceStore(blocked={CitationSourceType.MAIL}),
        )

    oversized_request = AskRequest(
        request_id="too-many-sources",
        query="Summarize my work",
        source_scopes=[CitationSourceType.WORK_ITEM, CitationSourceType.CALENDAR],
    )
    with pytest.raises(SourceScopeLimitExceeded):
        resolve_runtime_safety_controls(
            oversized_request,
            IDENTITY,
            governance_store=FakeGovernanceStore(max_sources=1),
        )
