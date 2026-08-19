from __future__ import annotations

from .contracts import RiskTier, WorkplaceAction, WorkplaceActionMode


class WorkplaceActionNotFound(RuntimeError):
    pass


class WorkplaceActionForbidden(RuntimeError):
    pass


_ACTIONS: tuple[WorkplaceAction, ...] = (
    WorkplaceAction(
        action_key="CALENDAR.EVENT.CREATE",
        title="Create calendar event",
        description="Open the Calendar event composer with a reviewed draft context.",
        mode=WorkplaceActionMode.REDIRECT,
        risk_tier=RiskTier.L1,
        required_permission="APP.CALENDAR:CREATE",
        target_route="/calendar/schedule?create=event",
        confirmation_required=True,
        input_fields=["title", "startsAt", "endsAt", "attendees"],
    ),
    WorkplaceAction(
        action_key="MAIL.DRAFT.CREATE",
        title="Draft an email",
        description="Open Mail compose so the user can review recipients and content before sending.",
        mode=WorkplaceActionMode.REDIRECT,
        risk_tier=RiskTier.L1,
        required_permission="APP.MAIL:CREATE",
        target_route="/mail/inbox?compose=open",
        confirmation_required=True,
        input_fields=["to", "subject", "body"],
    ),
    WorkplaceAction(
        action_key="SERVICE.REQUEST.CREATE",
        title="Start a service request",
        description="Open the employee service catalog and keep submission under user control.",
        mode=WorkplaceActionMode.REDIRECT,
        risk_tier=RiskTier.L1,
        required_permission="APP.EMPLOYEE_SERVICES:VIEW",
        target_route="/services/discover",
        confirmation_required=True,
        input_fields=["serviceCategory", "requestSummary"],
    ),
    WorkplaceAction(
        action_key="APPROVAL.REQUEST.CREATE",
        title="Prepare an approval request",
        description="Hand off a reviewed draft to the governed approval authoring workflow.",
        mode=WorkplaceActionMode.APPROVAL_HANDOFF,
        risk_tier=RiskTier.L2,
        required_permission="ACTION.APPROVAL_REQUEST:CREATE",
        target_route="/approvals/requests/new",
        confirmation_required=True,
        input_fields=["formType", "title", "businessJustification", "approvers"],
    ),
)


def available_workplace_actions(permissions: tuple[str, ...]) -> list[WorkplaceAction]:
    authorities = {permission.strip().upper() for permission in permissions}
    return [action for action in _ACTIONS if action.required_permission in authorities]


def resolve_workplace_action(action_key: str, permissions: tuple[str, ...]) -> WorkplaceAction:
    normalized = action_key.strip().upper()
    action = next((candidate for candidate in _ACTIONS if candidate.action_key == normalized), None)
    if action is None:
        raise WorkplaceActionNotFound("The requested workplace action is not registered.")
    if action.required_permission not in {
        permission.strip().upper() for permission in permissions
    }:
        raise WorkplaceActionForbidden("The workplace action is outside the verified permission scope.")
    return action
