from __future__ import annotations

import pytest

from dwp_agent.product_surface_pep import Binding, _enabled_for, resolve_binding
from dwp_agent.product_surface_pep_bindings import (
    PRODUCT_AUTHORIZATION_V30_CHECKSUM,
    ROUTE_BINDING_V30_SPECS,
)


EXPECTED_ROUTE_KEYS = {
    "route.dwaion.work.artifact-collaboration-remediation.action",
    "route.dwaion.work.attachment-audit-report.action",
    "route.dwaion.work.attachment-audit-report.data",
    "route.dwaion.work.attachment-detach.action",
    "route.dwaion.work.personal-deletion-evidence.action",
    "route.dwaion.work.personal-deletion-evidence-download.data",
    "route.dwaion.work.personal-deletion-evidence.data",
    "route.dwaion.work.proposal-handoff-draft.action",
    "route.dwaion.work.proposal-handoff-draft.data",
    "route.dwaion.work.research-recovery.action",
    "route.dwaion.work.research-pdf-download.data",
    "route.dwaion.work.routine-advanced-approval.action",
    "route.dwaion.work.routine-advanced-pending-approvals.data",
    "route.dwaion.work.routine-advanced.action",
    "route.dwaion.work.routine-advanced.data",
}


def test_v30_owner_projection_is_pinned_and_complete() -> None:
    assert PRODUCT_AUTHORIZATION_V30_CHECKSUM == (
        "7c437bd768225db7dfe1c2491bcdb6ff256a2376946bab6a4ba4a62a7f31ce80"
    )
    assert len(ROUTE_BINDING_V30_SPECS) == 18
    assert {
        spec["route_contract_key"] for spec in ROUTE_BINDING_V30_SPECS
    } == EXPECTED_ROUTE_KEYS
    assert len(
        {(spec["method"], spec["path_template"]) for spec in ROUTE_BINDING_V30_SPECS}
    ) == len(ROUTE_BINDING_V30_SPECS)
    assert {spec["introduced_version"] for spec in ROUTE_BINDING_V30_SPECS} == {30}


def test_v30_sensitive_bindings_require_exact_authority_and_parameters() -> None:
    routine_id = "00000000-0000-4000-8000-000000000001"
    pending = resolve_binding(
        "GET", "/v1/routines/advanced-commands/pending-approvals"
    )
    assert pending is not None
    assert pending.route_contract_key == (
        "route.dwaion.work.routine-advanced-pending-approvals.data"
    )
    assert pending.required_permission_sets == (
        frozenset({"APP.ASK:VIEW", "APP.DWAION_ROUTINES:APPROVE"}),
    )
    assert pending.scope_kind == "CONFIG_SCOPE"

    routine = resolve_binding("POST", f"/v1/routines/{routine_id}/advanced-commands")
    assert routine is not None
    assert routine.required_permission_sets == (
        frozenset({"APP.ASK:VIEW", "APP.DWAION_ROUTINES:MANAGE"}),
    )
    assert resolve_binding("POST", "/v1/routines/not-a-uuid/advanced-commands") is None

    decision = resolve_binding(
        "POST", f"/v1/routines/advanced-commands/{routine_id}/decision"
    )
    assert decision is not None
    assert decision.required_permission_sets == (
        frozenset({"APP.ASK:VIEW", "APP.DWAION_ROUTINES:APPROVE"}),
    )
    assert decision.scope_kind == "CONFIG_SCOPE"

    evidence = resolve_binding(
        "POST",
        f"/v1/personal-data/deletions/{routine_id}/evidence-actions/export-receipt",
    )
    assert evidence is not None
    assert evidence.required_permission_sets == (
        frozenset({"APP.DWAION_PRIVACY:MANAGE"}),
    )
    assert resolve_binding(
        "POST",
        f"/v1/personal-data/deletions/{routine_id}/evidence-actions/not$valid",
    ) is None

    research_recovery = resolve_binding(
        "POST", f"/v1/research/runs/{routine_id}/recovery-actions"
    )
    assert research_recovery is not None
    assert research_recovery.required_permission_sets == (
        frozenset({"APP.ASK:VIEW", "APP.DWAION_RESEARCH:MANAGE"}),
    )
    research_pdf = resolve_binding(
        "GET", f"/v1/research/runs/{routine_id}/downloads/pdf"
    )
    assert research_pdf is not None
    assert research_pdf.required_permission_sets == (
        frozenset({"APP.ASK:VIEW", "APP.DWAION_RESEARCH:VIEW"}),
    )


def test_v30_readiness_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V30_ENABLED", raising=False)
    binding = Binding("route.test.v30", "DATA", introduced_version=30)
    assert not _enabled_for(binding)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V30_ENABLED", "true")
    assert _enabled_for(binding)
