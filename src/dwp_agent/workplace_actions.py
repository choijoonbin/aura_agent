from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import JsonValue

from .contracts import RiskTier, WorkplaceAction, WorkplaceActionMode


class WorkplaceActionNotFound(RuntimeError):
    pass


class WorkplaceActionForbidden(RuntimeError):
    pass


class WorkplaceActionInputInvalid(RuntimeError):
    pass


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def review_workplace_action_inputs(
    action: WorkplaceAction, inputs: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    unknown = sorted(set(inputs) - set(action.input_fields))
    if unknown:
        raise WorkplaceActionInputInvalid(
            f"Unsupported input fields: {', '.join(unknown)}."
        )

    if action.action_key == "CALENDAR.EVENT.CREATE":
        reviewed = _review_calendar(inputs)
    elif action.action_key == "MAIL.DRAFT.CREATE":
        reviewed = _review_mail(inputs)
    elif action.action_key == "SERVICE.REQUEST.CREATE":
        reviewed = _review_service_request(inputs)
    elif action.action_key == "APPROVAL.REQUEST.CREATE":
        reviewed = _review_approval_request(inputs)
    else:
        raise WorkplaceActionInputInvalid("The action input contract is not registered.")
    return {key: value for key, value in reviewed.items() if value not in (None, "", [])}


def _review_calendar(inputs: dict[str, JsonValue]) -> dict[str, JsonValue]:
    starts_at = _datetime_value(inputs.get("startsAt"), "startsAt")
    ends_at = _datetime_value(inputs.get("endsAt"), "endsAt")
    if starts_at and ends_at and ends_at <= starts_at:
        raise WorkplaceActionInputInvalid("endsAt must be later than startsAt.")
    return {
        "title": _text(inputs.get("title"), "title", 300),
        "startsAt": _iso_datetime(starts_at),
        "endsAt": _iso_datetime(ends_at),
        "attendees": _emails(inputs.get("attendees"), "attendees"),
    }


def _review_mail(inputs: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "to": _emails(inputs.get("to"), "to"),
        "subject": _text(inputs.get("subject"), "subject", 500),
        "body": _text(inputs.get("body"), "body", 100_000),
    }


def _review_service_request(inputs: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "serviceCategory": _text(inputs.get("serviceCategory"), "serviceCategory", 100),
        "requestSummary": _text(inputs.get("requestSummary"), "requestSummary", 240),
    }


def _review_approval_request(inputs: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "formType": _text(inputs.get("formType"), "formType", 128),
        "title": _text(inputs.get("title"), "title", 300),
        "businessJustification": _text(
            inputs.get("businessJustification"), "businessJustification", 2_000
        ),
        "approvers": _emails(inputs.get("approvers"), "approvers"),
    }


def _text(value: JsonValue | None, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkplaceActionInputInvalid(f"{field} must be text.")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise WorkplaceActionInputInvalid(
            f"{field} must contain at most {max_length} characters."
        )
    return normalized or None


def _emails(value: JsonValue | None, field: str) -> list[str]:
    if value is None or value == "":
        return []
    candidates = [value] if isinstance(value, str) else value
    if not isinstance(candidates, list) or any(not isinstance(item, str) for item in candidates):
        raise WorkplaceActionInputInvalid(f"{field} must be an email address list.")
    if len(candidates) > 50:
        raise WorkplaceActionInputInvalid(f"{field} can contain at most 50 addresses.")
    normalized = list(dict.fromkeys(item.strip().lower() for item in candidates if item.strip()))
    if any(not _EMAIL_PATTERN.fullmatch(item) for item in normalized):
        raise WorkplaceActionInputInvalid(f"{field} contains an invalid email address.")
    return normalized


def _datetime_value(value: JsonValue | None, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise WorkplaceActionInputInvalid(f"{field} must be an ISO 8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise WorkplaceActionInputInvalid(
            f"{field} must be an ISO 8601 timestamp."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkplaceActionInputInvalid(f"{field} must include a time zone.")
    return parsed.astimezone(timezone.utc)


def _iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None
