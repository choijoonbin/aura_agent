from __future__ import annotations

import pytest

from dwp_agent.product_surface_pep import Binding, _enabled_for, resolve_binding
from dwp_agent.product_surface_pep_bindings import (
    PRODUCT_AUTHORIZATION_V24_CHECKSUM,
    ROUTE_BINDING_V24_SPECS,
)


def test_v24_owner_projection_is_pinned_and_complete() -> None:
    assert PRODUCT_AUTHORIZATION_V24_CHECKSUM == (
        "be3db891d27cd0b94aa88ac706d9bc87d4b991c9f9d8e505e26b296647728b84"
    )
    assert len(ROUTE_BINDING_V24_SPECS) == 12
    assert len({(item["method"], item["path_template"]) for item in ROUTE_BINDING_V24_SPECS}) == 12
    assert {item["introduced_version"] for item in ROUTE_BINDING_V24_SPECS} == {24}


def test_v24_sensitive_bindings_require_exact_authority_and_parameters() -> None:
    rollback = resolve_binding(
        "POST", "/v1/routines/00000000-0000-4000-8000-000000000001/versions/2/rollback"
    )
    assert rollback is not None
    assert rollback.required_permission_sets == (
        frozenset({"APP.DWAION_ROUTINES:MANAGE"}),
    )
    assert resolve_binding(
        "POST", "/v1/routines/not-a-uuid/versions/0/rollback"
    ) is None
    comment = resolve_binding(
        "POST", "/v1/artifact-collaboration/00000000-0000-4000-8000-000000000001/workspace/comments"
    )
    assert comment is not None
    assert comment.required_permission_sets == (
        frozenset({"APP.DWAION_ARTIFACTS:UPDATE"}),
    )


def test_v24_readiness_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V24_ENABLED", raising=False)
    binding = Binding("route.test.v24", "DATA", introduced_version=24)
    assert not _enabled_for(binding)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V24_ENABLED", "true")
    assert _enabled_for(binding)
