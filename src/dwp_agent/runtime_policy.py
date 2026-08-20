from __future__ import annotations

from .contracts import AskRequest
from .governance_contracts import SourceAccessMode
from .governance_store import GovernanceStore, get_governance_store
from .policy import AskIdentity, SafetyControls


class SourcePolicyBlocked(RuntimeError):
    def __init__(self, sources: tuple[str, ...]) -> None:
        self.sources = sources
        super().__init__(f"DWAI-ON source policy blocks: {', '.join(sources)}.")


class SourceScopeLimitExceeded(RuntimeError):
    def __init__(self, requested: int, allowed: int) -> None:
        self.requested = requested
        self.allowed = allowed
        super().__init__(f"At most {allowed} source scopes are allowed; received {requested}.")


def resolve_runtime_safety_controls(
    request: AskRequest,
    identity: AskIdentity,
    *,
    governance_store: GovernanceStore | None = None,
) -> SafetyControls:
    store = governance_store or get_governance_store()
    source_policies = store.source_policies(
        tenant_id=identity.tenant_id,
        actor_user_id=identity.user_id,
    )
    safety = store.safety_policy(
        tenant_id=identity.tenant_id,
        actor_user_id=identity.user_id,
    )
    allowed_sources = {
        policy.source_key
        for policy in source_policies
        if policy.enabled and policy.access_mode != SourceAccessMode.BLOCKED
    }
    blocked = tuple(sorted(
        scope.value for scope in request.source_scopes if scope not in allowed_sources
    ))
    if blocked:
        raise SourcePolicyBlocked(blocked)
    if len(request.source_scopes) > safety.max_source_scopes:
        raise SourceScopeLimitExceeded(len(request.source_scopes), safety.max_source_scopes)
    return SafetyControls(
        privileged_data_outcome=safety.privileged_data_outcome,
        mutation_outcome=safety.mutation_outcome,
    )
