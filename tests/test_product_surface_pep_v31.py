from __future__ import annotations

from dwp_agent.product_surface_pep import Binding, _enabled_for
from dwp_agent.product_surface_pep_bindings import (
    PRODUCT_AUTHORIZATION_V31_CHECKSUM,
    ROUTE_BINDING_V31_SPECS,
)


def test_v31_registry_seal_has_no_agent_owner_delta() -> None:
    assert PRODUCT_AUTHORIZATION_V31_CHECKSUM == (
        "be4e1b6db3d3f0b5100182a3c80066a39c64479f9ba88d908fee661efd3335b8"
    )
    assert ROUTE_BINDING_V31_SPECS == ()


def test_v31_readiness_enables_the_unchanged_v30_agent_projection(monkeypatch) -> None:
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V30_ENABLED", raising=False)
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V31_ENABLED", raising=False)
    binding = Binding("route.test.v30", "DATA", introduced_version=30)
    assert not _enabled_for(binding)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V31_ENABLED", "true")
    assert _enabled_for(binding)
