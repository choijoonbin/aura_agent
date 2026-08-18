from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from dwp_agent.mail_actions import (
    MAIL_ACTION_CONTRACT_VERSION,
    MAIL_ACTION_POLICIES,
    MailActionEvidence,
    MailActionKind,
    MailActionProposal,
    MailActionRisk,
    MailActionTarget,
)


def calendar_proposal(**overrides: object) -> MailActionProposal:
    values: dict[str, object] = {
        "proposal_id": UUID("6de8f8d8-16c9-4e1a-9f27-102d768e9ad0"),
        "tenant_id": 1,
        "user_id": 7,
        "thread_id": UUID("8a0b5388-8765-4c97-a95c-4037285416d8"),
        "action": MailActionKind.CREATE_CALENDAR_EVENT,
        "title": "Schedule the project review",
        "summary": "The participants proposed Thursday at 15:00.",
        "proposed_payload": {
            "title": "Project review",
            "durationMinutes": 30,
            "timeZone": "Asia/Seoul",
            "requiresConfirmation": True,
        },
        "evidence": [
            MailActionEvidence(
                source_message_id=UUID("c7f4f933-7b7d-488b-938d-b3c35bd07902"),
                observed_at=datetime(2030, 8, 18, 9, 0, tzinfo=timezone.utc),
                rationale="The sender explicitly proposed a meeting time.",
                excerpt_sha256="a" * 64,
            )
        ],
        "confidence": 0.91,
        "risk": MailActionRisk.MEDIUM,
        "target": MailActionTarget(
            resource_key="APP.CALENDAR",
            permission_code="CREATE",
            route="/calendar/schedule?action=create",
        ),
        "expires_at": datetime(2030, 8, 19, 9, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return MailActionProposal.model_validate(values)


def test_mail_action_requires_human_confirmation_and_disallows_auto_execution() -> None:
    proposal = calendar_proposal()

    assert proposal.contract_version == MAIL_ACTION_CONTRACT_VERSION
    assert proposal.human_confirmation_required is True
    assert proposal.automatic_execution_allowed is False

    with pytest.raises(ValidationError):
        calendar_proposal(automatic_execution_allowed=True)


def test_every_mail_action_has_an_explicit_governed_policy() -> None:
    assert set(MAIL_ACTION_POLICIES) == set(MailActionKind)
    assert MAIL_ACTION_POLICIES[MailActionKind.ESCALATE_NOTIFICATION].cross_application is False


def test_mail_action_rejects_cross_application_permission_confusion() -> None:
    with pytest.raises(ValidationError, match="governed resource policy"):
        calendar_proposal(
            target=MailActionTarget(
                resource_key="APP.HCM",
                permission_code="CREATE",
                route="/hr/absence?request=open",
            )
        )


def test_mail_action_enforces_risk_floor_and_evidence() -> None:
    with pytest.raises(ValidationError, match="risk is below"):
        calendar_proposal(risk=MailActionRisk.LOW)

    with pytest.raises(ValidationError):
        calendar_proposal(evidence=[])


def test_mail_action_rejects_unknown_versions_and_incomplete_payloads() -> None:
    with pytest.raises(ValidationError):
        calendar_proposal(contract_version=2)

    with pytest.raises(ValidationError, match="governed contract"):
        calendar_proposal(proposed_payload={"requiresConfirmation": True})

    with pytest.raises(ValidationError, match="explicit confirmation"):
        calendar_proposal(
            proposed_payload={
                "durationMinutes": 30,
                "timeZone": "Asia/Seoul",
                "requiresConfirmation": False,
            }
        )

    with pytest.raises(ValidationError):
        calendar_proposal(expires_at=datetime(2030, 8, 19, 9, 0))
