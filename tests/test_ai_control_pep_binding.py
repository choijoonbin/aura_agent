from __future__ import annotations

import pytest

from dwp_agent.product_surface_pep import (
    Binding,
    _enabled_for,
    owns_candidate,
    resolve_binding,
)
from dwp_agent.product_surface_pep_bindings import (
    PRODUCT_AUTHORIZATION_V21_CHECKSUM,
    ROUTE_BINDING_V21_SPECS,
)


EXPECTED_ROUTES = {
    ("GET", "/v1/admin/ai-control"): (
        "route.dwaion.management.ai-control.page",
        "DATA",
        (("ADMIN.DWAION_SAFETY:VIEW",), ("ADMIN.DWAION_SAFETY:MANAGE",)),
    ),
    ("POST", "/v1/admin/ai-control/bootstrap"): (
        "route.dwaion.management.ai-control-bootstrap.action",
        "ACTION",
        (("ADMIN.DWAION_SAFETY:UPDATE",), ("ADMIN.DWAION_SAFETY:MANAGE",)),
    ),
    ("PUT", "/v1/admin/ai-control/policy"): (
        "route.dwaion.management.ai-control-update.action",
        "ACTION",
        (("ADMIN.DWAION_SAFETY:UPDATE",), ("ADMIN.DWAION_SAFETY:MANAGE",)),
    ),
    ("POST", "/v1/admin/ai-control/emergency"): (
        "route.dwaion.management.ai-control-emergency.action",
        "ACTION",
        (("ADMIN.DWAION_SAFETY:MANAGE",),),
    ),
}


def test_v21_overlay_is_pinned_to_canonical_checksum_and_exact_contracts() -> None:
    assert PRODUCT_AUTHORIZATION_V21_CHECKSUM == (
        "4cd1732df91d197cc47fca94699b0fb702ab1f6f2c557d3d17ce0e069d65af85"
    )
    actual = {
        (spec["method"], spec["path_template"]): (
            spec["route_contract_key"],
            spec["route_kind"],
            spec["required_permission_sets"],
        )
        for spec in ROUTE_BINDING_V21_SPECS
    }
    assert actual == EXPECTED_ROUTES
    assert all(spec["introduced_version"] == 21 for spec in ROUTE_BINDING_V21_SPECS)
    assert all(spec["surface_key"] == "dwaion.management" for spec in ROUTE_BINDING_V21_SPECS)
    assert all(spec["scope_kind"] == "CONFIG_SCOPE" for spec in ROUTE_BINDING_V21_SPECS)


def test_v21_routes_resolve_exactly_and_wrong_methods_are_not_owned() -> None:
    for (method, path), (route_key, route_kind, permissions) in EXPECTED_ROUTES.items():
        assert owns_candidate(method, path)
        binding = resolve_binding(method, path)
        assert binding is not None
        assert binding.route_contract_key == route_key
        assert binding.route_kind == route_kind
        assert binding.introduced_version == 21
        assert binding.surface_key == "dwaion.management"
        assert binding.scope_kind == "CONFIG_SCOPE"
        assert binding.required_permission_sets == tuple(
            frozenset(permission_set) for permission_set in permissions
        )

    assert not owns_candidate("PATCH", "/v1/admin/ai-control/policy")
    assert resolve_binding("PATCH", "/v1/admin/ai-control/policy") is None


def test_v21_readiness_is_explicit_and_unknown_versions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for version in (4, 5, 6, 21):
        monkeypatch.delenv(
            f"DWP_AGENT_PRODUCT_AUTHORIZATION_V{version}_ENABLED", raising=False
        )

    v6 = Binding("route.test.v6", "DATA", introduced_version=6)
    v21 = Binding("route.test.v21", "DATA", introduced_version=21)
    unknown = Binding("route.test.unknown", "DATA", introduced_version=22)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V6_ENABLED", "true")
    assert _enabled_for(v6)
    assert not _enabled_for(v21)
    assert not _enabled_for(unknown)

    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V21_ENABLED", "true")
    assert _enabled_for(v21)
    assert not _enabled_for(unknown)
