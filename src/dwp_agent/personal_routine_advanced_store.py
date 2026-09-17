from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
    GovernedFingerprints,
    GovernedPayloadCodec,
    advisory_lock,
    require_command_replay,
)
from .personal_domain_security import PersonalDomainIdentity
from .personal_routine_advanced_contracts import (
    CreateRoutineAdvancedCommandRequest,
    DecideRoutineAdvancedCommandRequest,
    RoutineAdvancedCommand,
    RoutineAdvancedCommandKind,
    RoutineAdvancedCommandProblem,
    RoutineAdvancedCommandReceipt,
    RoutineAdvancedCommandState,
    RoutineChangeApprovalPayload,
)
from .personal_routine_advanced_provider import (
    HttpRoutineAdvancedProvider,
    RoutineAdvancedProviderContext,
    RoutineAdvancedProviderError,
    RoutineAdvancedProviderResult,
    ensure_provider_result_bound,
    execute_bound_provider,
    internal_change_approval_result,
    routine_advanced_capability,
)
from .personal_routine_advanced_decisions import (
    claim_checker_decision,
    load_advanced_command,
    record_advanced_event,
    require_checker_decision_replay,
)
from .personal_routine_advanced_effects import apply_advanced_runtime_effect
from .personal_routine_contracts import UpdateRoutineRequest
from .personal_routine_postgres_store import PostgresPersonalRoutineStore


class PersonalRoutineAdvancedCommandStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.routines = PostgresPersonalRoutineStore(database_url)
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise GovernedDomainUnavailable(
                "Advanced routine command security is unavailable."
            ) from error

    def create(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: CreateRoutineAdvancedCommandRequest,
    ) -> RoutineAdvancedCommand:
        payload = request.payload.model_dump(mode="json", by_alias=True)
        kind = request.payload.kind
        proof = self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose="personal-routine:advanced-command",
            payload={
                "routineId": str(routine_id),
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "routine-advanced-command",
                    identity.tenant_id,
                    identity.user_id,
                    request.command_id,
                )
                row = load_advanced_command(connection, identity.tenant_id, request.command_id)
                if row is not None:
                    require_command_replay(
                        row["session_fingerprint"], row["request_fingerprint"], proof
                    )
                    if row["routine_id"] != routine_id or row["user_id"] != identity.user_id:
                        raise GovernedDomainConflict(
                            "The advanced command ID is already bound to another routine."
                        )
                else:
                    routine = self.routines.get(identity, routine_id)
                    if routine.revision != request.expected_revision:
                        raise GovernedDomainConflict(
                            "The personal routine revision has changed."
                        )
                    capability = routine_advanced_capability(kind)
                    if not capability.available:
                        raise GovernedDomainUnavailable(
                            capability.reason_code
                            or "ROUTINE_ADVANCED_PROVIDER_UNAVAILABLE"
                        )
                    state = (
                        RoutineAdvancedCommandState.AWAITING_APPROVAL
                        if kind == RoutineAdvancedCommandKind.CHANGE_APPROVAL
                        else RoutineAdvancedCommandState.RUNNING
                    )
                    envelope = self.codec.encrypt_json(
                        payload,
                        tenant_id=identity.tenant_id,
                        resource_type="routine-advanced-command",
                        resource_id=str(request.command_id),
                        field="payload",
                    )
                    row = connection.execute(
                        """INSERT INTO ai_personal_routine_advanced_commands (
                               command_id, routine_id, tenant_id, user_id, kind, state,
                               expected_revision, maker_user_id, session_fingerprint,
                               request_fingerprint, payload_envelope)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *""",
                        (
                            request.command_id,
                            routine_id,
                            identity.tenant_id,
                            identity.user_id,
                            kind.value,
                            state.value,
                            request.expected_revision,
                            identity.user_id,
                            proof.session_fingerprint,
                            proof.request_fingerprint,
                            envelope,
                        ),
                    ).fetchone()
                    record_advanced_event(connection, identity, row, "REQUESTED", None)
                command = self._command(row, identity.user_id)
            if command.state != RoutineAdvancedCommandState.RUNNING:
                return command
            return self._execute_provider(identity, command, payload)
        except (
            GovernedDomainConflict,
            GovernedDomainNotFound,
            GovernedDomainUnavailable,
        ):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "The advanced routine command could not be created."
            ) from error

    def decide(
        self,
        identity: PersonalDomainIdentity,
        command_id: UUID,
        request: DecideRoutineAdvancedCommandRequest,
    ) -> RoutineAdvancedCommand:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = load_advanced_command(connection, identity.tenant_id, command_id, lock=True)
            if row is None:
                raise GovernedDomainNotFound("The routine approval command is unavailable.")
            if row["kind"] != RoutineAdvancedCommandKind.CHANGE_APPROVAL.value:
                raise GovernedDomainConflict("This routine command does not accept a decision.")
            if row["maker_user_id"] == identity.user_id:
                raise GovernedDomainConflict("Maker and checker must be different users.")
            if row["state"] in {"SUCCEEDED", "REJECTED", "FAILED"}:
                terminal_decision = (
                    "REJECT" if row["state"] == "REJECTED" else "APPROVE"
                )
                if (
                    row["checker_user_id"] != identity.user_id
                    or request.decision != terminal_decision
                ):
                    raise GovernedDomainConflict(
                        "The routine approval already has a terminal decision."
                    )
                require_checker_decision_replay(
                    connection, self.fingerprints, identity, row, request
                )
                return self._command(row, identity.user_id)
            if int(row["version"]) != request.expected_revision:
                raise GovernedDomainConflict("The routine approval version has changed.")
            if row["state"] != RoutineAdvancedCommandState.AWAITING_APPROVAL.value:
                raise GovernedDomainConflict("The routine approval is not awaiting a decision.")
            claim_checker_decision(
                connection, self.codec, self.fingerprints, identity, row, request
            )
            if request.decision == "REJECT":
                updated = connection.execute(
                    """UPDATE ai_personal_routine_advanced_commands
                          SET state = 'REJECTED', checker_user_id = %s,
                              version = version + 1, updated_at = CURRENT_TIMESTAMP
                        WHERE command_id = %s RETURNING *""",
                    (identity.user_id, command_id),
                ).fetchone()
                record_advanced_event(
                    connection, identity, updated, "REJECTED", row["state"]
                )
                return self._command(updated, identity.user_id)
            row = connection.execute(
                """UPDATE ai_personal_routine_advanced_commands
                      SET checker_user_id = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE command_id = %s RETURNING *""",
                (identity.user_id, command_id),
            ).fetchone()
            payload = self._payload(row)

        change = RoutineChangeApprovalPayload.model_validate(payload)
        owner_identity = PersonalDomainIdentity(
            tenant_id=identity.tenant_id,
            user_id=row["user_id"],
            correlation_id=identity.correlation_id,
            auth_session_id=f"routine-checker:{command_id}",
            roles=identity.roles,
            permissions=identity.permissions,
        )
        try:
            routine = self.routines.update(
                owner_identity,
                row["routine_id"],
                UpdateRoutineRequest(
                    command_id=uuid5(
                        NAMESPACE_URL,
                        f"urn:dwp:routine-advanced:{command_id}:approved-update",
                    ),
                    expected_revision=int(row["expected_revision"]),
                    reason_code="MAKER_CHECKER_APPROVED",
                    change_reason="An independent checker approved the routine definition change.",
                    definition=change.definition,
                ),
            )
        except GovernedDomainConflict:
            return self._fail(
                identity,
                command_id,
                RoutineAdvancedCommandProblem(
                    code="ROUTINE_APPROVAL_REVISION_CONFLICT",
                    detail="The routine changed before the approved definition could be applied.",
                    recovery_hint="Create a new approval request from the latest routine revision.",
                ),
            )
        provider = internal_change_approval_result(
            command_id=command_id, routine_id=routine.routine_id,
            expected_revision=int(row["expected_revision"]),
            applied_revision=routine.revision, payload=change,
            decision_id=request.command_id,
        )
        return self._finalize(
            identity,
            command_id,
            provider,
            checker=identity.user_id,
        )

    def list(
        self, identity: PersonalDomainIdentity, routine_id: UUID
    ) -> list[RoutineAdvancedCommand]:
        self.routines.get(identity, routine_id)
        with connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """SELECT * FROM ai_personal_routine_advanced_commands
                    WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
                    ORDER BY created_at DESC, command_id DESC""",
                (identity.tenant_id, identity.user_id, routine_id),
            ).fetchall()
        return [self._command(row, identity.user_id) for row in rows]

    def list_pending_for_checker(
        self, identity: PersonalDomainIdentity, *, limit: int
    ) -> list[RoutineAdvancedCommand]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """SELECT c.* FROM ai_personal_routine_advanced_commands c
                    WHERE c.tenant_id = %s
                      AND c.kind = 'CHANGE_APPROVAL'
                      AND c.state = 'AWAITING_APPROVAL'
                      AND c.maker_user_id <> %s
                      AND (
                          NOT EXISTS (
                              SELECT 1 FROM ai_personal_routine_advanced_decisions d
                               WHERE d.command_id = c.command_id
                          )
                          OR EXISTS (
                              SELECT 1 FROM ai_personal_routine_advanced_decisions d
                               WHERE d.command_id = c.command_id
                                 AND d.checker_user_id = %s
                                 AND d.decision = 'APPROVE'
                          )
                      )
                    ORDER BY created_at, command_id
                    LIMIT %s""",
                (identity.tenant_id, identity.user_id, identity.user_id, limit),
            ).fetchall()
        return [self._command(row, identity.user_id) for row in rows]

    def get(
        self, identity: PersonalDomainIdentity, routine_id: UUID, command_id: UUID
    ) -> RoutineAdvancedCommand:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = load_advanced_command(connection, identity.tenant_id, command_id)
        if row is None or row["routine_id"] != routine_id or row["user_id"] != identity.user_id:
            raise GovernedDomainNotFound("The advanced routine command is unavailable.")
        return self._command(row, identity.user_id)

    def _execute_provider(
        self,
        identity: PersonalDomainIdentity,
        command: RoutineAdvancedCommand,
        payload: dict[str, object],
    ) -> RoutineAdvancedCommand:
        try:
            result = execute_bound_provider(
                HttpRoutineAdvancedProvider(command.kind),
                RoutineAdvancedProviderContext(
                    command_id=command.command_id,
                    routine_id=command.routine_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    correlation_id=identity.correlation_id,
                    kind=command.kind,
                    expected_revision=command.expected_revision,
                    payload=payload,
                )
            )
            return self._finalize(identity, command.command_id, result)
        except RoutineAdvancedProviderError as error:
            problem = RoutineAdvancedCommandProblem(
                code=error.code,
                detail="The configured advanced routine provider did not complete the command.",
                recovery_hint=error.recovery_hint,
            )
            return self._fail(identity, command.command_id, problem)

    def _finalize(
        self,
        identity: PersonalDomainIdentity,
        command_id: UUID,
        result: RoutineAdvancedProviderResult,
        *,
        checker: str | None = None,
    ) -> RoutineAdvancedCommand:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = load_advanced_command(connection, identity.tenant_id, command_id, lock=True)
            if row is None:
                raise GovernedDomainNotFound("The advanced routine command is unavailable.")
            if row["state"] in {"SUCCEEDED", "PARTIAL"}:
                return self._command(row, identity.user_id)
            if result.kind != RoutineAdvancedCommandKind.CHANGE_APPROVAL:
                ensure_provider_result_bound(
                    RoutineAdvancedProviderContext(
                        command_id=row["command_id"], routine_id=row["routine_id"],
                        tenant_id=row["tenant_id"], user_id=row["user_id"],
                        correlation_id=identity.correlation_id, kind=RoutineAdvancedCommandKind(row["kind"]),
                        expected_revision=row["expected_revision"], payload=self._payload(row),
                    ),
                    result,
                )
            completed_at = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            receipt = RoutineAdvancedCommandReceipt(
                receipt_id=uuid4(),
                command_id=command_id,
                routine_id=row["routine_id"],
                kind=row["kind"],
                state=result.state,
                provider_receipt_id=result.provider_receipt_id,
                result_sha256=result.result_sha256,
                provider_outcome=result.result,
                applied_revision=result.applied_revision,
                completed_at=completed_at,
            )
            apply_advanced_runtime_effect(connection, row, result)
            problem = (
                RoutineAdvancedCommandProblem(
                    code=result.problem_code or "ROUTINE_ADVANCED_PARTIAL",
                    detail=result.problem_detail or "The provider completed only part of the command.",
                    recovery_hint=result.recovery_hint or "Review provider evidence before retrying.",
                )
                if result.state == "PARTIAL"
                else None
            )
            updated = self._update_terminal(
                connection, row, receipt.state.value, receipt, problem, checker
            )
            record_advanced_event(
                connection, identity, updated, "COMPLETED", row["state"]
            )
            return self._command(updated, identity.user_id)

    def _fail(
        self,
        identity: PersonalDomainIdentity,
        command_id: UUID,
        problem: RoutineAdvancedCommandProblem,
    ) -> RoutineAdvancedCommand:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = load_advanced_command(connection, identity.tenant_id, command_id, lock=True)
            if row is None:
                raise GovernedDomainNotFound("The advanced routine command is unavailable.")
            if row["state"] == "FAILED":
                return self._command(row, identity.user_id)
            updated = self._update_terminal(connection, row, "FAILED", None, problem, None)
            record_advanced_event(connection, identity, updated, "FAILED", row["state"])
            return self._command(updated, identity.user_id)

    def _update_terminal(
        self,
        connection: Any,
        row: Any,
        state: str,
        receipt: RoutineAdvancedCommandReceipt | None,
        problem: RoutineAdvancedCommandProblem | None,
        checker: str | None,
    ) -> Any:
        receipt_envelope = self._envelope(row, "receipt", receipt) if receipt else None
        problem_envelope = self._envelope(row, "problem", problem) if problem else None
        return connection.execute(
            """UPDATE ai_personal_routine_advanced_commands
                  SET state = %s, checker_user_id = COALESCE(%s, checker_user_id),
                      receipt_envelope = %s, problem_envelope = %s,
                      version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE command_id = %s RETURNING *""",
            (state, checker, receipt_envelope, problem_envelope, row["command_id"]),
        ).fetchone()

    def _command(self, row: Any, viewer_user_id: str) -> RoutineAdvancedCommand:
        receipt = self._decoded(row, "receipt", RoutineAdvancedCommandReceipt)
        problem = self._decoded(row, "problem", RoutineAdvancedCommandProblem)
        proposed_definition = (
            RoutineChangeApprovalPayload.model_validate(self._payload(row)).definition
            if row["kind"] == RoutineAdvancedCommandKind.CHANGE_APPROVAL.value
            else None
        )
        return RoutineAdvancedCommand(
            command_id=row["command_id"],
            routine_id=row["routine_id"],
            owner_user_id=row["user_id"],
            kind=row["kind"],
            state=row["state"],
            expected_revision=row["expected_revision"],
            version=row["version"],
            maker_user_id=row["maker_user_id"],
            checker_user_id=row["checker_user_id"],
            can_approve=(
                row["state"] == "AWAITING_APPROVAL"
                and row["maker_user_id"] != viewer_user_id
            ),
            proposed_definition=proposed_definition,
            problem=problem,
            receipt=receipt,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _payload(self, row: Any) -> dict[str, object]:
        return self.codec.decrypt_json(
            row["payload_envelope"],
            tenant_id=row["tenant_id"],
            resource_type="routine-advanced-command",
            resource_id=str(row["command_id"]),
            field="payload",
        )

    def _decoded(self, row: Any, field: str, model: Any) -> Any:
        envelope = row[f"{field}_envelope"]
        if envelope is None:
            return None
        return model.model_validate(
            self.codec.decrypt_json(
                envelope,
                tenant_id=row["tenant_id"],
                resource_type="routine-advanced-command",
                resource_id=str(row["command_id"]),
                field=field,
            )
        )

    def _envelope(self, row: Any, field: str, value: Any) -> str:
        return self.codec.encrypt_json(
            value.model_dump(mode="json", by_alias=True),
            tenant_id=row["tenant_id"],
            resource_type="routine-advanced-command",
            resource_id=str(row["command_id"]),
            field=field,
        )
@lru_cache(maxsize=1)
def get_personal_routine_advanced_store() -> PersonalRoutineAdvancedCommandStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise GovernedDomainUnavailable("Advanced routine commands are unavailable.")
    return PersonalRoutineAdvancedCommandStore(database_url)
