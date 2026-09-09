from __future__ import annotations

from dataclasses import dataclass

from .contracts import AskPersonalization, AskPersonalizationState
from .governed_domain_core import GovernedDomainUnavailable, tenant_number
from .personal_memory_contracts import RuntimeMemorySelection
from .personal_memory_store import get_personal_memory_store
from .policy import AskIdentity


_MEMORY_PERMISSION = "APP.DWAION_MEMORY:VIEW"


@dataclass(frozen=True)
class ResolvedPersonalization:
    state: AskPersonalizationState
    preferences: tuple[tuple[str, str], ...] = ()

    def evidence(self, *, model_applied: bool) -> AskPersonalization:
        state = self.state
        kinds: list[str] = []
        if self.preferences:
            state = AskPersonalizationState.APPLIED if model_applied else AskPersonalizationState.BYPASSED
            if model_applied:
                kinds = [kind for kind, _value in self.preferences]
        return AskPersonalization(state=state, applied_kinds=kinds)


class PersonalMemoryRuntime:
    def resolve(
        self, identity: AskIdentity, *, agent_key: str = "DWP_ASSISTANT"
    ) -> ResolvedPersonalization:
        if agent_key.strip().upper() != "DWP_ASSISTANT":
            return ResolvedPersonalization(AskPersonalizationState.NOT_EVALUATED)
        permissions = {permission.strip().upper() for permission in identity.permissions}
        if _MEMORY_PERMISSION not in permissions:
            return ResolvedPersonalization(AskPersonalizationState.NOT_PERMITTED)
        try:
            selection = get_personal_memory_store().runtime_preferences(
                tenant_id=tenant_number(identity.tenant_id),
                user_id=identity.user_id,
            )
        except GovernedDomainUnavailable:
            return ResolvedPersonalization(AskPersonalizationState.UNAVAILABLE)
        return _resolved(selection)


def _resolved(selection: RuntimeMemorySelection) -> ResolvedPersonalization:
    if not selection.storage_enabled or not selection.runtime_enabled:
        return ResolvedPersonalization(AskPersonalizationState.DISABLED)
    preferences = tuple((memory.kind.value, memory.memory.value) for memory in selection.memories)
    if not preferences:
        return ResolvedPersonalization(AskPersonalizationState.EMPTY)
    return ResolvedPersonalization(AskPersonalizationState.APPLIED, preferences)
