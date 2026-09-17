from __future__ import annotations

import pytest

from dwp_agent.product_surface_pep import Binding, _enabled_for, owns_candidate, resolve_binding
from dwp_agent.product_surface_pep_bindings import (
    PRODUCT_AUTHORIZATION_V22_CHECKSUM,
    ROUTE_BINDING_V22_SPECS,
)


EXPECTED_ROUTE_KEYS = {
    "route.dwaion.management.control-plane-command.action",
    "route.dwaion.management.control-plane-commands.data",
    "route.dwaion.management.control-plane-snapshots.data",
    "route.dwaion.management.models-routing.page",
    "route.dwaion.work.activity.page",
    "route.dwaion.work.agents.page",
    "route.dwaion.work.artifact-collaboration-access-request.action",
    "route.dwaion.work.artifact-collaboration-edit.action",
    "route.dwaion.work.artifact-collaboration-members.action",
    "route.dwaion.work.artifact-collaboration-preflight.action",
    "route.dwaion.work.artifact-collaboration-resolve.action",
    "route.dwaion.work.artifact-collaboration-share.action",
    "route.dwaion.work.artifact-collaboration-workspace.action",
    "route.dwaion.work.artifact-collaboration.data",
    "route.dwaion.work.attachment-create.action",
    "route.dwaion.work.attachment-delete.action",
    "route.dwaion.work.attachments.data",
    "route.dwaion.work.conversation-detail.page",
    "route.dwaion.work.conversations.page",
    "route.dwaion.work.new.page",
    "route.dwaion.work.personal-deletion-retry.action",
    "route.dwaion.work.personal-deletions.data",
    "route.dwaion.work.proposal-handoff.action",
    "route.dwaion.work.proposal-handoff.data",
    "route.dwaion.work.research-deliveries.data",
    "route.dwaion.work.research-output.action",
    "route.dwaion.work.research-plan-create.action",
    "route.dwaion.work.research-plan-update.action",
    "route.dwaion.work.research-plans.data",
    "route.dwaion.work.research-run-command.action",
    "route.dwaion.work.research-run-execute.action",
    "route.dwaion.work.research-run-start.action",
    "route.dwaion.work.research-runs.data",
    "route.dwaion.work.routine-activation.action",
    "route.dwaion.work.routine-execution.data",
    "route.dwaion.work.routine-run-command.action",
    "route.dwaion.work.routine-run-trigger.action",
}


def test_v22_owner_projection_is_pinned_and_complete() -> None:
    assert PRODUCT_AUTHORIZATION_V22_CHECKSUM == (
        "1629b75f62c7bb524dc70faaecac73499b9f9b0fb126ab38221b6e4773f35ede"
    )
    assert len(ROUTE_BINDING_V22_SPECS) == 61
    assert {spec["route_contract_key"] for spec in ROUTE_BINDING_V22_SPECS} == EXPECTED_ROUTE_KEYS
    method_paths = {(spec["method"], spec["path_template"]) for spec in ROUTE_BINDING_V22_SPECS}
    assert len(method_paths) == len(ROUTE_BINDING_V22_SPECS)
    assert ("GET", "/v1/attachments/{attachmentId}/evidence") in method_paths
    assert ("GET", "/v1/research/capabilities") in method_paths
    assert all(spec["introduced_version"] == 22 for spec in ROUTE_BINDING_V22_SPECS)


def test_v22_sensitive_bindings_resolve_with_exact_authority_and_parameters() -> None:
    attachment = resolve_binding("GET", "/v1/attachments/00000000-0000-4000-8000-000000000001/evidence")
    assert attachment is not None
    assert attachment.route_contract_key == "route.dwaion.work.attachments.data"
    assert attachment.required_permission_sets == (frozenset({"APP.ASK:VIEW"}),)
    assert resolve_binding("GET", "/v1/attachments/not-a-uuid/evidence") is None

    research_read = resolve_binding("GET", "/v1/research/capabilities")
    assert research_read is not None
    assert research_read.required_permission_sets == (
        frozenset({"APP.ASK:VIEW", "APP.DWAION_RESEARCH:VIEW"}),
    )
    research_write = resolve_binding("POST", "/v1/research/plans")
    assert research_write is not None
    assert research_write.required_permission_sets == (
        frozenset({"APP.ASK:VIEW", "APP.DWAION_RESEARCH:MANAGE"}),
    )
    for granted in (
        frozenset({"APP.ASK:VIEW"}),
        frozenset({"APP.DWAION_RESEARCH:MANAGE"}),
    ):
        assert not any(
            required.issubset(granted)
            for required in research_write.required_permission_sets
        )

    command = resolve_binding("POST", "/v1/admin/control-plane/commands")
    assert command is not None
    assert command.route_contract_key == "route.dwaion.management.control-plane-command.action"
    assert command.required_permission_sets == (
        frozenset({"ADMIN.DWAION_OPERATIONS:MANAGE"}),
    )
    assert command.scope_kind == "CONFIG_SCOPE"
    assert not owns_candidate("DELETE", "/v1/admin/control-plane/commands")


def test_v22_readiness_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V22_ENABLED", raising=False)
    binding = Binding("route.test.v22", "DATA", introduced_version=22)
    assert not _enabled_for(binding)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V22_ENABLED", "true")
    assert _enabled_for(binding)
