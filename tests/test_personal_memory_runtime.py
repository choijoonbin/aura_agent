from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from dwp_agent import personal_memory_runtime as runtime_module
from dwp_agent.governed_domain_core import GovernedDomainUnavailable
from dwp_agent.personal_memory_contracts import (
    ExplicitMemoryValue,
    PersonalMemory,
    RuntimeMemorySelection,
)
from dwp_agent.personal_memory_runtime import PersonalMemoryRuntime
from dwp_agent.policy import AskIdentity


def _identity(*permissions: str) -> AskIdentity:
    return AskIdentity(
        tenant_id="7001",
        user_id="member-1",
        roles=("WORKSPACE_MEMBER",),
        permissions=permissions,
        correlation_id="memory-runtime-test",
    )


def _memory(kind: str, value: str) -> PersonalMemory:
    now = datetime.now(UTC)
    return PersonalMemory(
        memory_id=uuid4(),
        kind=kind,
        state="ACTIVE",
        revision=1,
        memory=ExplicitMemoryValue(value=value),
        created_at=now,
        updated_at=now,
    )


class _Store:
    def __init__(self, selection: RuntimeMemorySelection) -> None:
        self.selection = selection
        self.calls: list[tuple[int, str]] = []

    def runtime_preferences(self, *, tenant_id: int, user_id: str) -> RuntimeMemorySelection:
        self.calls.append((tenant_id, user_id))
        return self.selection


def test_runtime_applies_only_explicit_available_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(
        RuntimeMemorySelection(
            True,
            True,
            (
                _memory("TONE", "Use a concise professional tone"),
                _memory("OUTPUT_FORMAT", "Use numbered actions"),
            ),
        )
    )
    monkeypatch.setattr(runtime_module, "get_personal_memory_store", lambda: store)

    resolved = PersonalMemoryRuntime().resolve(
        _identity("APP.DWAION_MEMORY:VIEW")
    )

    assert resolved.preferences == (
        ("TONE", "Use a concise professional tone"),
        ("OUTPUT_FORMAT", "Use numbered actions"),
    )
    assert resolved.evidence(model_applied=True).model_dump(by_alias=True) == {
        "state": "APPLIED",
        "appliedKinds": ["TONE", "OUTPUT_FORMAT"],
    }
    assert store.calls == [(7001, "member-1")]


def test_runtime_fails_closed_without_permission_or_explicit_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(RuntimeMemorySelection(True, False, ()))
    monkeypatch.setattr(runtime_module, "get_personal_memory_store", lambda: store)
    runtime = PersonalMemoryRuntime()

    denied = runtime.resolve(_identity("APP.ASK:VIEW"))
    disabled = runtime.resolve(_identity("APP.DWAION_MEMORY:VIEW"))

    assert denied.state == "NOT_PERMITTED"
    assert disabled.state == "DISABLED"
    assert store.calls == [(7001, "member-1")]


def test_non_assistant_agent_never_reads_personal_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(RuntimeMemorySelection(True, True, (_memory("TONE", "Concise"),)))
    monkeypatch.setattr(runtime_module, "get_personal_memory_store", lambda: store)

    resolved = PersonalMemoryRuntime().resolve(
        _identity("APP.DWAION_MEMORY:VIEW"), agent_key="DWP_APPROVAL_EXPERT"
    )

    assert resolved.state == "NOT_EVALUATED"
    assert store.calls == []


def test_optional_personalization_outage_does_not_claim_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable():
        raise GovernedDomainUnavailable("database unavailable")

    monkeypatch.setattr(runtime_module, "get_personal_memory_store", unavailable)

    resolved = PersonalMemoryRuntime().resolve(_identity("APP.DWAION_MEMORY:VIEW"))

    assert resolved.state == "UNAVAILABLE"
    assert resolved.evidence(model_applied=False).applied_kinds == []
