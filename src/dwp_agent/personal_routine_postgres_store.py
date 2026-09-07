from __future__ import annotations

from datetime import UTC, datetime
from functools import wraps
from typing import Callable, TypeVar
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
    retention_deadline,
)
from .personal_domain_security import PersonalDomainIdentity
from .personal_routine_contracts import (
    ArchiveRoutineRequest,
    ChangeRoutineConsentRequest,
    CreateRoutineRequest,
    DryRunRoutineRequest,
    PersonalRoutine,
    RoutineConsentState,
    RoutineDryRunReceipt,
    RoutineLifecycle,
    UpdateRoutineRequest,
)
from .personal_routine_lifecycle import PersonalRoutineLifecycleCommands
from .personal_routine_postgres_base import (
    PersonalRoutinePostgresBase,
    _ROUTINE_SELECT,
)
from .personal_routine_schedule import preview_next_run


T = TypeVar("T")


def _translated(function: Callable[..., T]) -> Callable[..., T]:
    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> T:
        try:
            return function(*args, **kwargs)
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable("Personal routines are unavailable.") from error

    return wrapped


class PostgresPersonalRoutineStore(
    PersonalRoutineLifecycleCommands, PersonalRoutinePostgresBase
):
    change_lifecycle = _translated(PersonalRoutineLifecycleCommands.change_lifecycle)

    @_translated
    def list(self, identity: PersonalDomainIdentity) -> list[PersonalRoutine]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                _ROUTINE_SELECT
                + " WHERE tenant_id = %s AND user_id = %s ORDER BY updated_at DESC",
                (identity.tenant_id, identity.user_id),
            ).fetchall()
            return [self._routine(row) for row in rows]

    @_translated
    def get(self, identity: PersonalDomainIdentity, routine_id: UUID) -> PersonalRoutine:
        with connect(self.database_url, row_factory=dict_row) as connection:
            return self._routine(self._locked(connection, identity, routine_id, lock=False))

    @_translated
    def create(
        self, identity: PersonalDomainIdentity, request: CreateRoutineRequest
    ) -> PersonalRoutine:
        self._require_source_access(identity, request.definition)
        if request.expected_revision != 0:
            raise GovernedDomainConflict("A routine must start at revision zero.")
        proof = self._proof(identity, "CREATE", None, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "CREATE", proof)
            if replay is not None:
                return PersonalRoutine.model_validate(replay)
            self._require_source_preferences(connection, identity, request.definition)
            retention_days = self._retention_days(connection, identity.tenant_id)
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            routine_id = uuid4()
            envelope = self.codec.encrypt_json(
                request.definition.model_dump(mode="json", by_alias=True),
                tenant_id=identity.tenant_id,
                resource_type="personal-routine",
                resource_id=str(routine_id),
                field="definition",
            )
            definition_fingerprint = self._definition_fingerprint(
                identity.tenant_id, request.definition
            )
            connection.execute(
                """INSERT INTO ai_personal_routines (
                       routine_id, tenant_id, user_id, definition_envelope,
                       definition_fingerprint, retention_until, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    routine_id,
                    identity.tenant_id,
                    identity.user_id,
                    envelope,
                    definition_fingerprint,
                    retention_deadline(now, retention_days),
                    now,
                    now,
                ),
            )
            self._replace_sources(connection, identity, routine_id, request.definition)
            routine = self._routine(self._locked(connection, identity, routine_id, lock=False))
            self._record_command(connection, identity, routine_id, "CREATE", request, proof, routine)
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                "CREATED",
                None,
                routine.lifecycle_state.value,
                routine.revision,
                request.reason_code,
                proof.request_fingerprint,
                None,
            )
            return routine

    @_translated
    def update(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: UpdateRoutineRequest,
    ) -> PersonalRoutine:
        self._require_source_access(identity, request.definition)
        proof = self._proof(identity, "UPDATE", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "UPDATE", proof)
            if replay is not None:
                return PersonalRoutine.model_validate(replay)
            self._require_source_preferences(connection, identity, request.definition)
            row = self._locked(connection, identity, routine_id)
            self._expected(row, request.expected_revision)
            if row["lifecycle_state"] == RoutineLifecycle.ARCHIVED.value:
                raise GovernedDomainConflict("An archived routine cannot be changed.")
            fingerprint = self._definition_fingerprint(identity.tenant_id, request.definition)
            consent_values = self._consent_values(row)
            reconsent = (
                row["definition_fingerprint"] != fingerprint
                and RoutineConsentState.ENABLED.value in consent_values.values()
            )
            if reconsent:
                consent_values = {
                    key: RoutineConsentState.RECONSENT_REQUIRED.value
                    if value == RoutineConsentState.ENABLED.value
                    else value
                    for key, value in consent_values.items()
                }
            next_consent = self._aggregate_consent(consent_values)
            envelope = self.codec.encrypt_json(
                request.definition.model_dump(mode="json", by_alias=True),
                tenant_id=identity.tenant_id,
                resource_type="personal-routine",
                resource_id=str(routine_id),
                field="definition",
            )
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE ai_personal_routines
                      SET definition_envelope = %s, definition_fingerprint = %s,
                          consent_state = %s, source_access_consent_state = %s,
                          analysis_consent_state = %s,
                          proposal_delivery_consent_state = %s,
                          revision = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
                (
                    envelope,
                    fingerprint,
                    next_consent,
                    consent_values["SOURCE_ACCESS"],
                    consent_values["ANALYSIS"],
                    consent_values["PROPOSAL_DELIVERY"],
                    revision,
                    routine_id,
                    identity.tenant_id,
                    identity.user_id,
                ),
            )
            self._replace_sources(connection, identity, routine_id, request.definition)
            if reconsent:
                self._consent_event(
                    connection,
                    identity,
                    routine_id,
                    request.command_id,
                    row["consent_state"],
                    next_consent,
                    revision,
                    fingerprint,
                    proof,
                    request.reason_code,
                    "Routine definition changed; explicit consent must be renewed.",
                    "ALL",
                )
            routine = self._routine(self._locked(connection, identity, routine_id, lock=False))
            self._record_command(connection, identity, routine_id, "UPDATE", request, proof, routine)
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                "UPDATED",
                row["lifecycle_state"],
                routine.lifecycle_state.value,
                revision,
                request.reason_code,
                proof.request_fingerprint,
                None,
            )
            return routine

    @_translated
    def change_consent(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: ChangeRoutineConsentRequest,
    ) -> PersonalRoutine:
        proof = self._proof(identity, "CONSENT", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "CONSENT", proof)
            if replay is not None:
                return PersonalRoutine.model_validate(replay)
            row = self._locked(connection, identity, routine_id)
            self._expected(row, request.expected_revision)
            if row["lifecycle_state"] == RoutineLifecycle.ARCHIVED.value:
                raise GovernedDomainConflict("An archived routine cannot receive consent.")
            if request.consent_state == RoutineConsentState.ENABLED:
                definition = self._definition(row)
                self._require_source_access(identity, definition)
                self._require_source_preferences(connection, identity, definition)
            consent_values = self._consent_values(row)
            previous_scope_state = consent_values[request.scope.value]
            consent_values[request.scope.value] = request.consent_state.value
            aggregate = self._aggregate_consent(consent_values)
            consent_column = {
                "SOURCE_ACCESS": "source_access_consent_state",
                "ANALYSIS": "analysis_consent_state",
                "PROPOSAL_DELIVERY": "proposal_delivery_consent_state",
            }[request.scope.value]
            revision = int(row["revision"]) + 1
            connection.execute(
                f"""UPDATE ai_personal_routines
                      SET {consent_column} = %s, consent_state = %s,
                          revision = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
                (
                    request.consent_state.value,
                    aggregate,
                    revision,
                    routine_id,
                    identity.tenant_id,
                    identity.user_id,
                ),
            )
            self._consent_event(
                connection,
                identity,
                routine_id,
                request.command_id,
                previous_scope_state,
                request.consent_state.value,
                revision,
                row["definition_fingerprint"],
                proof,
                request.reason_code,
                request.change_reason,
                request.scope.value,
            )
            routine = self._routine(self._locked(connection, identity, routine_id, lock=False))
            self._record_command(connection, identity, routine_id, "CONSENT", request, proof, routine)
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                "CONSENT_CHANGED",
                row["consent_state"],
                aggregate,
                revision,
                request.reason_code,
                proof.request_fingerprint,
                request.change_reason,
            )
            return routine

    @_translated
    def dry_run(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: DryRunRoutineRequest,
    ) -> RoutineDryRunReceipt:
        proof = self._proof(identity, "DRY_RUN", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "DRY_RUN", proof)
            if replay is not None:
                return RoutineDryRunReceipt.model_validate(replay)
            row = self._locked(connection, identity, routine_id)
            self._expected(row, request.expected_revision)
            consents = self._consent_values(row)
            required = {"SOURCE_ACCESS", "ANALYSIS"}
            if any(
                consents[scope] != RoutineConsentState.ENABLED.value
                for scope in required
            ):
                raise GovernedDomainConflict(
                    "Source-access and analysis consent are required for a dry run."
                )
            if row["lifecycle_state"] != RoutineLifecycle.DRAFT.value:
                raise GovernedDomainConflict(
                    "Only an unpaused draft routine can be validated."
                )
            definition = self._definition(row)
            self._require_source_access(identity, definition)
            self._require_source_preferences(connection, identity, definition)
            reference = request.reference_time or datetime.now(UTC)
            next_run = preview_next_run(definition, after=reference)
            evaluated_at = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            run_id = uuid4()
            receipt = RoutineDryRunReceipt(
                routine_run_id=run_id,
                routine_id=routine_id,
                routine_revision=row["revision"],
                validated_sources=definition.sources,
                preview_next_run_at=next_run,
                evaluated_at=evaluated_at,
                evidence_count=len(definition.sources),
            )
            connection.execute(
                """INSERT INTO ai_personal_routine_runs (
                       routine_run_id, routine_id, tenant_id, user_id, command_id,
                       trigger_type, run_state, routine_revision, source_count,
                       proposal_only, preview_next_run_at)
                   VALUES (%s, %s, %s, %s, %s, 'DRY_RUN', 'VALIDATED', %s, %s, TRUE, %s)""",
                (
                    run_id,
                    routine_id,
                    identity.tenant_id,
                    identity.user_id,
                    request.command_id,
                    row["revision"],
                    len(definition.sources),
                    next_run,
                ),
            )
            self._record_command(connection, identity, routine_id, "DRY_RUN", request, proof, receipt)
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                "DRY_RUN_VALIDATED",
                row["lifecycle_state"],
                row["lifecycle_state"],
                row["revision"],
                request.reason_code,
                proof.request_fingerprint,
                None,
            )
            return receipt

    @_translated
    def archive(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: ArchiveRoutineRequest,
    ) -> PersonalRoutine:
        proof = self._proof(identity, "ARCHIVE", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "ARCHIVE", proof)
            if replay is not None:
                return PersonalRoutine.model_validate(replay)
            row = self._locked(connection, identity, routine_id)
            self._expected(row, request.expected_revision)
            if row["lifecycle_state"] == RoutineLifecycle.ARCHIVED.value:
                raise GovernedDomainConflict("The routine is already archived.")
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE ai_personal_routines
                      SET lifecycle_state = 'ARCHIVED', consent_state = 'DISABLED',
                          source_access_consent_state = 'DISABLED',
                          analysis_consent_state = 'DISABLED',
                          proposal_delivery_consent_state = 'DISABLED',
                          revision = %s, archived_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
                (revision, routine_id, identity.tenant_id, identity.user_id),
            )
            self._consent_event(
                connection,
                identity,
                routine_id,
                request.command_id,
                row["consent_state"],
                RoutineConsentState.DISABLED.value,
                revision,
                row["definition_fingerprint"],
                proof,
                request.reason_code,
                request.change_reason,
                "ALL",
            )
            routine = self._routine(self._locked(connection, identity, routine_id, lock=False))
            self._record_command(connection, identity, routine_id, "ARCHIVE", request, proof, routine)
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                "ARCHIVED",
                row["lifecycle_state"],
                RoutineLifecycle.ARCHIVED.value,
                revision,
                request.reason_code,
                proof.request_fingerprint,
                request.change_reason,
            )
            return routine
