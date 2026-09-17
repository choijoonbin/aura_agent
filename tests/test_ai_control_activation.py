from __future__ import annotations

import pytest
from fastapi import HTTPException

import dwp_agent.ai_control_integration as integration
from dwp_agent.ai_control_runtime import AIControlUnavailable


class RuntimeStub:
    def __init__(self, **kwargs) -> None:
        self.ai_runtime_control = kwargs.get("ai_runtime_control")


def test_disabled_activation_preserves_legacy_ask_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DWP_AI_RUNTIME_CONTROL_ENFORCEMENT_ENABLED", raising=False)
    monkeypatch.setattr(integration, "AskRuntime", RuntimeStub)
    monkeypatch.setattr(
        integration,
        "get_ai_control_store",
        lambda: pytest.fail("Disabled enforcement must not require the AI control store."),
    )

    runtime = integration.controlled_ask_runtime()

    assert runtime.ai_runtime_control is None


def test_enabled_activation_injects_control_and_fails_closed_without_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_AI_RUNTIME_CONTROL_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setattr(integration, "AskRuntime", RuntimeStub)
    store = object()
    monkeypatch.setattr(integration, "get_ai_control_store", lambda: store)

    runtime = integration.controlled_ask_runtime()

    assert runtime.ai_runtime_control is not None
    assert runtime.ai_runtime_control.store is store

    monkeypatch.setattr(
        integration,
        "get_ai_control_store",
        lambda: (_ for _ in ()).throw(AIControlUnavailable("store unavailable")),
    )
    with pytest.raises(HTTPException) as raised:
        integration.controlled_ask_runtime()
    assert raised.value.status_code == 503
