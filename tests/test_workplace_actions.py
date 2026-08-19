import pytest

from dwp_agent.contracts import (
    AgentRegistryResolution,
    PlanPreviewRequest,
    RegistryResolutionStatus,
    RegistryRiskTier,
)
from dwp_agent.planner import build_reference_plan
from dwp_agent.workplace_actions import (
    WorkplaceActionInputInvalid,
    resolve_workplace_action,
    review_workplace_action_inputs,
)


REGISTRY = AgentRegistryResolution(
    entry_key="REFERENCE_PLANNER",
    revision=1,
    artifact_version="test-v1",
    risk_tier=RegistryRiskTier.MEDIUM,
    resolution=RegistryResolutionStatus.ACTIVE,
)


def action(action_key: str):
    permission = {
        "CALENDAR.EVENT.CREATE": "APP.CALENDAR:CREATE",
        "MAIL.DRAFT.CREATE": "APP.MAIL:CREATE",
        "SERVICE.REQUEST.CREATE": "APP.EMPLOYEE_SERVICES:VIEW",
        "APPROVAL.REQUEST.CREATE": "ACTION.APPROVAL_REQUEST:CREATE",
    }[action_key]
    return resolve_workplace_action(action_key, (permission,))


def test_calendar_inputs_are_normalized_and_bounded() -> None:
    reviewed = review_workplace_action_inputs(
        action("CALENDAR.EVENT.CREATE"),
        {
            "title": "  Weekly review  ",
            "startsAt": "2026-08-20T10:00:00+09:00",
            "endsAt": "2026-08-20T11:00:00+09:00",
            "attendees": [" LEAD@SK.COM ", "lead@sk.com"],
        },
    )

    assert reviewed == {
        "title": "Weekly review",
        "startsAt": "2026-08-20T01:00:00Z",
        "endsAt": "2026-08-20T02:00:00Z",
        "attendees": ["lead@sk.com"],
    }


def test_unknown_or_unsafe_action_inputs_are_rejected() -> None:
    with pytest.raises(WorkplaceActionInputInvalid, match="Unsupported input fields"):
        review_workplace_action_inputs(
            action("MAIL.DRAFT.CREATE"), {"subject": "Review", "sendImmediately": True}
        )
    with pytest.raises(WorkplaceActionInputInvalid, match="later than"):
        review_workplace_action_inputs(
            action("CALENDAR.EVENT.CREATE"),
            {
                "startsAt": "2026-08-20T11:00:00Z",
                "endsAt": "2026-08-20T10:00:00Z",
            },
        )
    with pytest.raises(WorkplaceActionInputInvalid, match="invalid email"):
        review_workplace_action_inputs(
            action("APPROVAL.REQUEST.CREATE"), {"approvers": ["not-an-email"]}
        )


def test_reviewed_inputs_are_bound_to_the_plan_hash() -> None:
    first = _plan({"subject": "First draft"})
    second = _plan({"subject": "Second draft"})

    assert first.plan_hash != second.plan_hash
    assert first.mutation_allowed is False
    assert first.approval_required is True


def _plan(inputs: dict[str, str]):
    return build_reference_plan(
        PlanPreviewRequest(
            request_id="handoff-1",
            intent="Prepare governed handoff for MAIL.DRAFT.CREATE",
            action="MAIL.DRAFT.CREATE",
            target="/mail/inbox?compose=open",
            inputs=inputs,
            agent_key="REFERENCE_PLANNER",
        ),
        tenant_id="1",
        user_id="user-1",
        roles=["WORKSPACE_MEMBER"],
        correlation_id="correlation-1",
        agent_registry=REGISTRY,
    )
