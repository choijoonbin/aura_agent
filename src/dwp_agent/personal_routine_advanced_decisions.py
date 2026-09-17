from __future__ import annotations

from uuid import UUID, uuid4
from typing import Any

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedFingerprints,
    GovernedPayloadCodec,
)
from .personal_domain_security import PersonalDomainIdentity
from .personal_routine_advanced_contracts import DecideRoutineAdvancedCommandRequest


def load_advanced_command(
    connection: Any, tenant_id: int, command_id: UUID, *, lock: bool = False
) -> Any:
    suffix = " FOR UPDATE" if lock else ""
    return connection.execute(
        "SELECT * FROM ai_personal_routine_advanced_commands WHERE tenant_id = %s AND command_id = %s"
        + suffix,
        (tenant_id, command_id),
    ).fetchone()


def _decision_proof(
    fingerprints: GovernedFingerprints,
    identity: PersonalDomainIdentity,
    row: Any,
    request: DecideRoutineAdvancedCommandRequest,
):
    return fingerprints.command(
        tenant_id=identity.tenant_id,
        session_id=identity.auth_session_id,
        purpose="personal-routine:advanced-checker-decision",
        payload={
            "advancedCommandId": str(row["command_id"]),
            "request": request.model_dump(mode="json", by_alias=True),
        },
    )


def require_checker_decision_replay(
    connection: Any,
    fingerprints: GovernedFingerprints,
    identity: PersonalDomainIdentity,
    row: Any,
    request: DecideRoutineAdvancedCommandRequest,
) -> None:
    existing = connection.execute(
        """SELECT routine_id, tenant_id, maker_user_id, checker_user_id,
                  decision, expected_command_version, request_fingerprint
             FROM ai_personal_routine_advanced_decisions
            WHERE command_id = %s
              FOR UPDATE""",
        (row["command_id"],),
    ).fetchone()
    proof = _decision_proof(fingerprints, identity, row, request)
    if existing is None or not _same_claim(
        existing, identity, row, request, proof.request_fingerprint
    ):
        raise GovernedDomainConflict(
            "The routine approval is already bound to another checker decision request."
        )


def claim_checker_decision(
    connection: Any,
    codec: GovernedPayloadCodec,
    fingerprints: GovernedFingerprints,
    identity: PersonalDomainIdentity,
    row: Any,
    request: DecideRoutineAdvancedCommandRequest,
) -> None:
    proof = _decision_proof(fingerprints, identity, row, request)
    existing = connection.execute(
        """SELECT routine_id, tenant_id, maker_user_id, checker_user_id,
                  decision, expected_command_version, request_fingerprint
             FROM ai_personal_routine_advanced_decisions
            WHERE command_id = %s
              FOR UPDATE""",
        (row["command_id"],),
    ).fetchone()
    if existing is not None:
        if not _same_claim(existing, identity, row, request, proof.request_fingerprint):
            raise GovernedDomainConflict(
                "The routine approval is already claimed by another checker or decision."
            )
        return

    payload = request.model_dump(mode="json", by_alias=True)
    envelope = codec.encrypt_json(
        payload,
        tenant_id=identity.tenant_id,
        resource_type="routine-advanced-decision",
        resource_id=str(row["command_id"]),
        field="decision",
    )
    connection.execute(
        """INSERT INTO ai_personal_routine_advanced_decisions (
               decision_id, command_id, routine_id, tenant_id,
               maker_user_id, checker_user_id, decision,
               expected_command_version, session_fingerprint,
               request_fingerprint, decision_envelope)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            request.command_id,
            row["command_id"],
            row["routine_id"],
            identity.tenant_id,
            row["maker_user_id"],
            identity.user_id,
            request.decision,
            request.expected_revision,
            proof.session_fingerprint,
            proof.request_fingerprint,
            envelope,
        ),
    )


def _same_claim(
    existing: Any,
    identity: PersonalDomainIdentity,
    row: Any,
    request: DecideRoutineAdvancedCommandRequest,
    request_fingerprint: str,
) -> bool:
    return (
        existing["routine_id"] == row["routine_id"]
        and int(existing["tenant_id"]) == identity.tenant_id
        and existing["maker_user_id"] == row["maker_user_id"]
        and existing["checker_user_id"] == identity.user_id
        and existing["decision"] == request.decision
        and int(existing["expected_command_version"]) == request.expected_revision
        and GovernedFingerprints.matches(
            existing["request_fingerprint"], request_fingerprint
        )
    )
def record_advanced_event(
    connection: Any,
    identity: PersonalDomainIdentity,
    row: Any,
    event_type: str,
    previous: str | None,
) -> None:
    connection.execute(
        """INSERT INTO ai_personal_routine_advanced_events (
               event_id, command_id, routine_id, tenant_id, user_id,
               actor_user_id, correlation_id, event_type, previous_state,
               current_state, version)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            uuid4(), row["command_id"], row["routine_id"], row["tenant_id"],
            row["user_id"], identity.user_id, identity.correlation_id,
            event_type, previous, row["state"], row["version"],
        ),
    )
