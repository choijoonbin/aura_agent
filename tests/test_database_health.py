import os
from urllib.parse import urlparse

import pytest

import dwp_agent.database_health as database_health
from dwp_agent.database_migrations import apply_migrations


def test_probe_preserves_failed_startup_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DWP_AGENT_DATABASE_URL", "postgresql://agent@database.internal/dwp_agent"
    )
    monkeypatch.setattr(database_health, "database_status", lambda: "FAILED")
    monkeypatch.setattr(
        database_health,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("A failed startup must not be upgraded."),
    )

    assert database_health.probe_database_status() == "FAILED"


def test_probe_reports_disabled_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DWP_AGENT_DATABASE_URL", raising=False)
    monkeypatch.setattr(database_health, "database_status", lambda: "DISABLED")

    assert database_health.probe_database_status() == "DISABLED"


@pytest.mark.integration
def test_probe_verifies_live_database_and_required_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Health integration tests require a dedicated test database.")
    apply_migrations(database_url)
    monkeypatch.setenv("DWP_AGENT_DATABASE_URL", database_url)
    monkeypatch.setattr(database_health, "database_status", lambda: "READY")

    assert database_health.probe_database_status() == "READY"
