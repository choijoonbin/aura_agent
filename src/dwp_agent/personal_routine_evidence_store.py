from __future__ import annotations

import os
from functools import lru_cache, wraps
from typing import Any, Callable, TypeVar
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity
from .personal_routine_contracts import (
    PersonalRoutine,
    RoutineExecutionRun,
    RoutineLifecycle,
    RoutineTriggerType,
)
from .personal_routine_evidence_contracts import (
    RollbackRoutineVersionRequest,
    RoutineHealth,
    RoutineHealthState,
    RoutineRollbackReceipt,
    RoutineVersionSnapshot,
    TriggerRoutineWebhookRequest,
)
from .personal_routine_evidence_integrity import evidence_integrity, telemetry_event
from .personal_routine_execution_queries import (
    EXECUTION_SELECT,
    PersonalRoutineExecutionQueries,
)
from .personal_routine_postgres_base import PersonalRoutinePostgresBase


T = TypeVar("T")


def _translated(function: Callable[..., T]) -> Callable[..., T]:
    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> T:
        try:
            return function(*args, **kwargs)
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable("Routine evidence is unavailable.") from error

    return wrapped


class PersonalRoutineEvidenceStore(
    PersonalRoutineExecutionQueries, PersonalRoutinePostgresBase
):
    @_translated
    def trigger_webhook(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: TriggerRoutineWebhookRequest,
    ) -> RoutineExecutionRun:
        proof = self._proof(identity, "WEBHOOK_TRIGGER", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(
                connection, identity, request.command_id, "WEBHOOK_TRIGGER", proof
            )
            if replay is not None:
                return RoutineExecutionRun.model_validate(replay)
            routine = self._locked(connection, identity, routine_id)
            self._expected(routine, request.expected_revision)
            if routine["lifecycle_state"] != RoutineLifecycle.ACTIVE.value:
                raise GovernedDomainConflict("Only an active routine accepts webhook events.")
            if not self.execution_available():
                raise GovernedDomainUnavailable("Governed routine execution is unavailable.")
            definition = self._definition(routine)
            if definition.trigger_type != RoutineTriggerType.WEBHOOK:
                raise GovernedDomainConflict(
                    "Only a webhook-triggered routine accepts webhook events."
                )
            if definition.webhook_event_type != request.event_type:
                raise GovernedDomainConflict(
                    "The webhook event type does not match the governed routine trigger."
                )
            self._require_source_access(identity, definition)
            self._require_source_preferences(connection, identity, definition)
            now = connection.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
            self._require_monthly_run_budget(
                connection, identity.tenant_id, identity.user_id, routine_id,
                definition.budget.maximum_runs_per_month, now,
            )
            payload_fingerprint = self.fingerprints.value(
                tenant_id=identity.tenant_id,
                purpose="personal-routine-webhook-payload",
                payload=request.payload,
            )
            duplicate = connection.execute(
                """SELECT webhook_payload_fingerprint
                     FROM ai_personal_routine_executions
                    WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
                      AND webhook_event_id = %s""",
                (identity.tenant_id, identity.user_id, routine_id, request.event_id),
            ).fetchone()
            if duplicate is not None:
                raise GovernedDomainConflict("The webhook event was already accepted.")
            run_id = uuid4()
            connection.execute(
                """INSERT INTO ai_personal_routine_executions (
                       routine_run_id, routine_id, tenant_id, user_id,
                       routine_revision, trigger_type, scheduled_for,
                       maximum_attempts, correlation_id, webhook_event_id,
                       webhook_event_type, webhook_occurred_at,
                       webhook_payload_fingerprint)
                   VALUES (%s, %s, %s, %s, %s, 'WEBHOOK', %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id, routine_id, identity.tenant_id, identity.user_id,
                    routine["revision"], now, definition.retry_policy.maximum_attempts,
                    identity.correlation_id, request.event_id, request.event_type,
                    request.occurred_at, payload_fingerprint,
                ),
            )
            self._execution_event(
                connection, run_id=run_id, routine_id=routine_id,
                tenant_id=identity.tenant_id, user_id=identity.user_id,
                event_type="QUEUED", previous_state=None, current_state="QUEUED",
                version=1, attempt_count=0, lease_generation=0,
            )
            run = self._execution(
                connection,
                self._locked_execution(connection, identity, routine_id, run_id, lock=False),
            )
            self._record_command(
                connection, identity, routine_id, "WEBHOOK_TRIGGER", request, proof, run,
            )
            self._event(
                connection, identity, routine_id, request.command_id,
                "WEBHOOK_TRIGGERED", routine["lifecycle_state"],
                routine["lifecycle_state"], routine["revision"], request.reason_code,
                proof.request_fingerprint, request.change_reason,
            )
            return run

    @_translated
    def rollback(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        target_revision: int,
        request: RollbackRoutineVersionRequest,
    ) -> RoutineRollbackReceipt:
        proof = self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose="personal-routine:rollback",
            payload={
                "routineId": str(routine_id),
                "targetRevision": target_revision,
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(
                connection, identity, request.command_id, "ROLLBACK", proof
            )
            if replay is not None:
                return RoutineRollbackReceipt.model_validate(replay)
            current = self._locked(connection, identity, routine_id)
            self._expected(current, request.expected_revision)
            if current["lifecycle_state"] == RoutineLifecycle.ARCHIVED.value:
                raise GovernedDomainConflict("An archived routine cannot be rolled back.")
            target, target_fingerprint = self._version_target(
                connection, identity, routine_id, target_revision
            )
            definition = target.definition
            self._require_source_access(identity, definition)
            self._require_source_preferences(connection, identity, definition)
            definition_fingerprint = self._definition_fingerprint(
                identity.tenant_id, definition
            )
            consent_values = self._consent_values(current)
            reconsent = (
                current["definition_fingerprint"] != definition_fingerprint
                and "ENABLED" in consent_values.values()
            )
            if reconsent:
                consent_values = {
                    key: "RECONSENT_REQUIRED" if value == "ENABLED" else value
                    for key, value in consent_values.items()
                }
            aggregate = self._aggregate_consent(consent_values)
            envelope = self.codec.encrypt_json(
                definition.model_dump(mode="json", by_alias=True),
                tenant_id=identity.tenant_id,
                resource_type="personal-routine",
                resource_id=str(routine_id),
                field="definition",
            )
            revision = int(current["revision"]) + 1
            connection.execute(
                """UPDATE ai_personal_routines
                      SET definition_envelope = %s, definition_fingerprint = %s,
                          consent_state = %s, source_access_consent_state = %s,
                          analysis_consent_state = %s,
                          proposal_delivery_consent_state = %s,
                          lifecycle_state = CASE
                              WHEN lifecycle_state = 'ACTIVE' THEN 'PAUSED'
                              ELSE lifecycle_state END,
                          execution_mode = CASE
                              WHEN lifecycle_state = 'ACTIVE' THEN 'DRY_RUN_ONLY'
                              ELSE execution_mode END,
                          next_run_at = NULL, revision = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
                (
                    envelope, definition_fingerprint, aggregate,
                    consent_values["SOURCE_ACCESS"], consent_values["ANALYSIS"],
                    consent_values["PROPOSAL_DELIVERY"], revision, routine_id,
                    identity.tenant_id, identity.user_id,
                ),
            )
            self._replace_sources(connection, identity, routine_id, definition)
            routine = self._routine(
                self._locked(connection, identity, routine_id, lock=False)
            )
            if reconsent:
                self._consent_event(
                    connection, identity, routine_id, request.command_id,
                    current["consent_state"], aggregate, revision,
                    definition_fingerprint, proof, request.reason_code,
                    "Routine version rollback changed the governed definition; "
                    "explicit consent must be renewed.", "ALL",
                )
            receipt_payload = {
                "commandId": str(request.command_id),
                "routineId": str(routine_id),
                "targetRevision": target_revision,
                "targetFingerprint": target_fingerprint,
                "createdRevision": routine.revision,
                "routine": routine.model_dump(mode="json", by_alias=True),
                "rolledBackAt": routine.updated_at.isoformat(),
            }
            receipt = RoutineRollbackReceipt.model_validate({
                **receipt_payload,
                "integrityFingerprint": evidence_integrity(receipt_payload),
            })
            self._record_rollback_command(
                connection, identity, routine_id, request, proof, receipt,
                target_revision, target_fingerprint,
            )
            self._event(
                connection, identity, routine_id, request.command_id,
                "VERSION_ROLLED_BACK", current["lifecycle_state"],
                routine.lifecycle_state.value, revision, request.reason_code,
                proof.request_fingerprint, request.change_reason,
            )
            return receipt

    @_translated
    def versions(
        self, identity: PersonalDomainIdentity, routine_id: UUID
    ) -> list[RoutineVersionSnapshot]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            self._locked(connection, identity, routine_id, lock=False)
            rows = connection.execute(
                """SELECT command_id, command_type, result_envelope, created_at,
                          rollback_target_revision, rollback_target_fingerprint
                     FROM ai_personal_routine_commands
                    WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
                      AND command_type IN (
                          'CREATE', 'UPDATE', 'CONSENT', 'LIFECYCLE',
                          'ACTIVATION', 'ARCHIVE', 'ROLLBACK')
                 ORDER BY created_at DESC, command_id DESC""",
                (identity.tenant_id, identity.user_id, routine_id),
            ).fetchall()
            result: list[RoutineVersionSnapshot] = []
            seen: set[int] = set()
            for row in rows:
                stored = self.codec.decrypt_json(
                        row["result_envelope"], tenant_id=identity.tenant_id,
                        resource_type="personal-routine-command",
                        resource_id=str(row["command_id"]), field="result",
                    )
                snapshot = (
                    RoutineRollbackReceipt.model_validate(stored).routine
                    if row["command_type"] == "ROLLBACK"
                    else PersonalRoutine.model_validate(stored)
                )
                if snapshot.revision in seen:
                    continue
                seen.add(snapshot.revision)
                payload = {
                    "commandId": str(row["command_id"]),
                    "commandType": row["command_type"],
                    "revision": snapshot.revision,
                    "snapshot": snapshot.model_dump(mode="json", by_alias=True),
                    "createdAt": row["created_at"].isoformat(),
                    "rollbackTargetRevision": row["rollback_target_revision"],
                    "rollbackTargetFingerprint": row["rollback_target_fingerprint"],
                }
                result.append(RoutineVersionSnapshot.model_validate(
                    {**payload, "integrityFingerprint": evidence_integrity(payload)}
                ))
            return result

    def _version_target(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        target_revision: int,
    ) -> tuple[PersonalRoutine, str]:
        rows = connection.execute(
            """SELECT command_id, command_type, result_envelope, created_at,
                      rollback_target_revision, rollback_target_fingerprint
                 FROM ai_personal_routine_commands
                WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
                  AND command_type IN (
                      'CREATE', 'UPDATE', 'CONSENT', 'LIFECYCLE',
                      'ACTIVATION', 'ARCHIVE', 'ROLLBACK')
             ORDER BY created_at DESC, command_id DESC""",
            (identity.tenant_id, identity.user_id, routine_id),
        ).fetchall()
        for row in rows:
            stored = self.codec.decrypt_json(
                    row["result_envelope"], tenant_id=identity.tenant_id,
                    resource_type="personal-routine-command",
                    resource_id=str(row["command_id"]), field="result",
                )
            snapshot = (
                RoutineRollbackReceipt.model_validate(stored).routine
                if row["command_type"] == "ROLLBACK"
                else PersonalRoutine.model_validate(stored)
            )
            if snapshot.revision != target_revision:
                continue
            payload = {
                "commandId": str(row["command_id"]),
                "commandType": row["command_type"],
                "revision": snapshot.revision,
                "snapshot": snapshot.model_dump(mode="json", by_alias=True),
                "createdAt": row["created_at"].isoformat(),
                "rollbackTargetRevision": row["rollback_target_revision"],
                "rollbackTargetFingerprint": row["rollback_target_fingerprint"],
            }
            return snapshot, evidence_integrity(payload)
        raise GovernedDomainNotFound("The routine version is unavailable.")

    def _record_rollback_command(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: RollbackRoutineVersionRequest,
        proof: Any,
        result: RoutineRollbackReceipt,
        target_revision: int,
        target_fingerprint: str,
    ) -> None:
        result_envelope = self.codec.encrypt_json(
            result.model_dump(mode="json", by_alias=True),
            tenant_id=identity.tenant_id,
            resource_type="personal-routine-command",
            resource_id=str(request.command_id),
            field="result",
        )
        connection.execute(
            """INSERT INTO ai_personal_routine_commands (
                   tenant_id, user_id, command_id, routine_id, command_type,
                   session_fingerprint, request_fingerprint, result_envelope,
                   rollback_target_revision, rollback_target_fingerprint)
               VALUES (%s, %s, %s, %s, 'ROLLBACK', %s, %s, %s, %s, %s)""",
            (
                identity.tenant_id, identity.user_id, request.command_id, routine_id,
                proof.session_fingerprint, proof.request_fingerprint, result_envelope,
                target_revision, target_fingerprint,
            ),
        )

    @_translated
    def health(self, identity: PersonalDomainIdentity, routine_id: UUID) -> RoutineHealth:
        with connect(self.database_url, row_factory=dict_row) as connection:
            routine = self._locked(connection, identity, routine_id, lock=False)
            latest = connection.execute(
                EXECUTION_SELECT
                + " WHERE tenant_id = %s AND user_id = %s AND routine_id = %s"
                + " ORDER BY created_at DESC, routine_run_id DESC LIMIT 1",
                (identity.tenant_id, identity.user_id, routine_id),
            ).fetchone()
            checked_at = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            worker = self.execution_available()
            consents = self._consent_values(routine)
            all_consents = all(value == "ENABLED" for value in consents.values())
            definition = self._definition(routine)
            schedule_current = routine["lifecycle_state"] != "ACTIVE" or (
                definition.trigger_type == RoutineTriggerType.SCHEDULED
                and routine["execution_mode"] == "SCHEDULED"
                and routine["next_run_at"] is not None
            ) or (
                definition.trigger_type == RoutineTriggerType.WEBHOOK
                and routine["execution_mode"] == "WEBHOOK"
                and routine["next_run_at"] is None
            )
            hints: list[str] = []
            if not worker:
                hints.append("Configure and start the governed routine execution worker.")
            if not all_consents:
                hints.append("Renew every required routine consent scope.")
            if not schedule_current:
                hints.append("Repair the governed routine trigger configuration.")
            if latest and latest["run_state"] in {"PARTIAL", "FAILED"}:
                hints.append(latest["recovery_hint"] or "Review and retry the latest run.")
            blocked = routine["lifecycle_state"] == "ACTIVE" and (
                not worker or not all_consents or not schedule_current
            )
            degraded = bool(latest and latest["run_state"] in {"PARTIAL", "FAILED", "CANCELLED"})
            state = (
                RoutineHealthState.BLOCKED if blocked
                else RoutineHealthState.DEGRADED if degraded
                else RoutineHealthState.HEALTHY
            )
            return RoutineHealth(
                routine_id=routine_id, routine_revision=routine["revision"], state=state,
                worker_available=worker, schedule_current=schedule_current,
                all_consents_enabled=all_consents,
                latest_run_id=latest["routine_run_id"] if latest else None,
                latest_run_state=latest["run_state"] if latest else None,
                latest_run_at=latest["updated_at"] if latest else None,
                recovery_hints=hints, checked_at=checked_at,
            )

    @_translated
    def telemetry(
        self, identity: PersonalDomainIdentity, routine_id: UUID
    ) -> list[RoutineTelemetryEvent]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            routine = self._locked(connection, identity, routine_id)
            self._record_download_event(connection, identity, routine)
            routine_events = connection.execute(
                """SELECT event_id, NULL::UUID AS routine_run_id, event_type,
                          previous_state, current_state, revision AS version, occurred_at
                     FROM ai_personal_routine_events
                    WHERE tenant_id = %s AND user_id = %s AND routine_id = %s""",
                (identity.tenant_id, identity.user_id, routine_id),
            ).fetchall()
            execution_events = connection.execute(
                """SELECT event_id, routine_run_id, event_type, previous_state,
                          current_state, version, occurred_at
                     FROM ai_personal_routine_execution_events
                    WHERE tenant_id = %s AND user_id = %s AND routine_id = %s""",
                (identity.tenant_id, identity.user_id, routine_id),
            ).fetchall()
            items = [
                telemetry_event("ROUTINE", row) for row in routine_events
            ] + [
                telemetry_event("EXECUTION", row) for row in execution_events
            ]
            return sorted(items, key=lambda item: (item.occurred_at, str(item.event_id)))

    def _record_download_event(
        self, connection: Any, identity: PersonalDomainIdentity, routine: Any
    ) -> None:
        command_id = uuid4()
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="personal-routine-evidence-download",
            payload={"routineId": str(routine["routine_id"]), "revision": routine["revision"]},
        )
        connection.execute(
            """INSERT INTO ai_personal_routine_events (
                   event_id, routine_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint, reason_code)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'EVIDENCE_DOWNLOADED',
                       %s, %s, %s, %s, 'USER_ROUTINE_EVIDENCE_DOWNLOAD')""",
            (
                uuid4(), routine["routine_id"], identity.tenant_id, identity.user_id,
                identity.user_id, identity.correlation_id, command_id,
                routine["lifecycle_state"], routine["lifecycle_state"],
                routine["revision"], fingerprint,
            ),
        )

@lru_cache(maxsize=1)
def get_personal_routine_evidence_store() -> PersonalRoutineEvidenceStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise GovernedDomainUnavailable("Personal routine database is unavailable.")
    return PersonalRoutineEvidenceStore(database_url)
