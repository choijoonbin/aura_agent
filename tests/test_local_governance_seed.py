from __future__ import annotations

import pytest

from dwp_agent.local_governance_seed import (
    LocalGovernanceSeedConfigurationError,
    _tenant_ids,
    seed_local_governance,
)


def test_local_governance_seed_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DWP_AGENT_LOCAL_GOVERNANCE_SEED_ENABLED", raising=False)

    assert seed_local_governance() == ()


def test_local_governance_seed_rejects_non_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_AGENT_LOCAL_GOVERNANCE_SEED_ENABLED", "true")
    monkeypatch.setenv("DWP_ENVIRONMENT", "production")

    with pytest.raises(LocalGovernanceSeedConfigurationError, match="only"):
        seed_local_governance()


@pytest.mark.parametrize("value", ["0", "1,1", "tenant-1", "1,-2"])
def test_local_governance_seed_rejects_invalid_tenant_ids(value: str) -> None:
    with pytest.raises(LocalGovernanceSeedConfigurationError):
        _tenant_ids(value)


def test_local_governance_seed_parses_unique_positive_tenant_ids() -> None:
    assert _tenant_ids("1, 7,42") == (1, 7, 42)
