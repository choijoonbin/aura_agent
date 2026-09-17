from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import (
    ResearchProgress,
    ResearchResult,
    ResearchRun,
    ResearchRunCommandAction,
    ResearchRunCommandRequest,
    ResearchRunState,
    ResearchWorkerObservation,
    StartResearchRunRequest,
)
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec, advisory_lock
from .personal_domain_security import PersonalDomainIdentity
from .research_plan_store import ResearchPlanStore, get_research_plan_store


class ResearchRunStore:
    def __init__(
        self,
        database_url: str,
        plan_store: ResearchPlanStore | None = None,
    ) -> None:
        self.database_url = database_url
        self.plan_store = plan_store or get_research_plan_store()
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise DwaionWorkflowUnavailable("Research run encryption is unavailable.") from error

    def start(
        self,
        identity: PersonalDomainIdentity,
        plan_id: UUID,
        request: StartResearchRunRequest,
    ) -> ResearchRun:
        plan = self.plan_store.get(identity, plan_id)
        if plan.revision != request.expected_plan_revision:
            raise DwaionWorkflowConflict("The research plan revision has changed.")
        if plan.state.value != "READY":
            raise DwaionWorkflowConflict("The research plan is not ready to run.")
        fingerprint = self._fingerprint(identity, "start", request)
        state = ResearchRunState.QUEUED if research_worker_configured() else ResearchRunState.PARTIAL
        progress = ResearchProgress(
            completed_steps=0,
            total_steps=max(2, len(plan.definition.source_policies) + 2),
            discovered_sources=0,
            verified_citations=0,
            failed_sources=[],
            recovery_hint=(
                None
                if state == ResearchRunState.QUEUED
                else "Configure the Deep Research worker, model route, and governed source connectors."
            ),
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(connection, "research-run", identity.tenant_id, identity.user_id, request.command_id)
                existing = connection.execute(
                    _SELECT + " WHERE r.tenant_id = %s AND r.user_id = %s AND (r.command_id = %s OR r.idempotency_key = %s)",
                    (identity.tenant_id, identity.user_id, request.command_id, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["plan_id"] != plan_id or int(existing["plan_revision"]) != request.expected_plan_revision:
                        raise DwaionWorkflowConflict("The research run key is already bound to another request.")
                    return self._record(existing)
                run_id = uuid4()
                progress_envelope = self._payload(identity, run_id, "progress", progress.model_dump(mode="json", by_alias=True))
                row = connection.execute(
                    """INSERT INTO ai_research_runs (
                           run_id, plan_id, tenant_id, user_id, command_id,
                           idempotency_key, run_state, plan_revision,
                           progress_envelope, safe_error_code)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *""",
                    (
                        run_id, plan_id, identity.tenant_id, identity.user_id,
                        request.command_id, request.idempotency_key, state.value,
                        plan.revision, progress_envelope,
                        None if state == ResearchRunState.QUEUED else "RESEARCH_WORKER_NOT_CONFIGURED",
                    ),
                ).fetchone()
                self._event(connection, identity, row, request.command_id, "START_REQUESTED", None, fingerprint, {"workerConfigured": research_worker_configured()})
                return self._record(row)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research run storage is unavailable.") from error

    def get(self, identity: PersonalDomainIdentity, run_id: UUID) -> ResearchRun:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = self._locked(connection, identity, run_id, lock=False)
                return self._record(row)
        except DwaionWorkflowNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research run storage is unavailable.") from error

    def command(
        self,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        request: ResearchRunCommandRequest,
    ) -> ResearchRun:
        fingerprint = self._fingerprint(identity, "command", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = self._locked(connection, identity, run_id)
                replay = self._event_replay(connection, identity, request.command_id, fingerprint)
                if replay:
                    return self._record(row)
                if int(row["revision"]) != request.expected_version:
                    raise DwaionWorkflowConflict("The research run version has changed.")
                current = ResearchRunState(row["run_state"])
                target = _command_target(current, request.action)
                progress = self._decode(row, "progress")
                if request.action == ResearchRunCommandAction.EXTEND:
                    progress["recoveryHint"] = f"Budget extended by {request.extension_minutes} minutes."
                elif request.action in {
                    ResearchRunCommandAction.EXCLUDE_SOURCE_AND_CONTINUE,
                    ResearchRunCommandAction.REPROBE_SOURCE,
                }:
                    progress["recoveryHint"] = f"{request.action.value}: {request.source_key}"
                if target == ResearchRunState.QUEUED and not research_worker_configured():
                    target = ResearchRunState.PARTIAL
                    progress["recoveryHint"] = "Deep Research worker is not configured."
                updated = connection.execute(
                    """UPDATE ai_research_runs
                          SET run_state = %s, revision = revision + 1,
                              progress_envelope = %s,
                              safe_error_code = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = %s
                    RETURNING *""",
                    (
                        target.value,
                        self._payload(identity, run_id, "progress", progress),
                        "RESEARCH_WORKER_NOT_CONFIGURED" if target == ResearchRunState.PARTIAL and not research_worker_configured() else None,
                        run_id,
                    ),
                ).fetchone()
                self._event(connection, identity, updated, request.command_id, request.action.value, current.value, fingerprint, {"reason": request.reason, "sourceKey": request.source_key})
                return self._record(updated)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research run storage is unavailable.") from error

    def observe(
        self,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        request: ResearchWorkerObservation,
    ) -> ResearchRun:
        fingerprint = self._fingerprint(identity, "observe", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = self._locked(connection, identity, run_id)
                replay = self._event_replay(connection, identity, request.command_id, fingerprint)
                if replay:
                    return self._record(row)
                if int(row["revision"]) != request.expected_version:
                    raise DwaionWorkflowConflict("The research run version has changed.")
                current = ResearchRunState(row["run_state"])
                _worker_transition(current, request.state)
                result_envelope = (
                    self._payload(identity, run_id, "result", request.result.model_dump(mode="json", by_alias=True))
                    if request.result else None
                )
                receipt_id = uuid4() if request.state == ResearchRunState.COMPLETED else None
                updated = connection.execute(
                    """UPDATE ai_research_runs
                          SET run_state = %s, revision = revision + 1,
                              progress_envelope = %s, result_envelope = %s,
                              receipt_id = %s, safe_error_code = %s,
                              started_at = CASE WHEN %s = 'RUNNING' AND started_at IS NULL THEN CURRENT_TIMESTAMP ELSE started_at END,
                              completed_at = CASE WHEN %s = 'COMPLETED' THEN CURRENT_TIMESTAMP ELSE NULL END,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = %s
                    RETURNING *""",
                    (
                        request.state.value,
                        self._payload(identity, run_id, "progress", request.progress.model_dump(mode="json", by_alias=True)),
                        result_envelope, receipt_id, request.safe_error_code,
                        request.state.value, request.state.value, run_id,
                    ),
                ).fetchone()
                self._event(connection, identity, updated, request.command_id, "WORKER_OBSERVED", current.value, fingerprint, {"safeErrorCode": request.safe_error_code})
                return self._record(updated)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research run storage is unavailable.") from error

    def _locked(self, connection: Any, identity: PersonalDomainIdentity, run_id: UUID, *, lock: bool = True) -> Any:
        row = connection.execute(
            _SELECT + " WHERE r.run_id = %s AND r.tenant_id = %s AND r.user_id = %s" + (" FOR UPDATE" if lock else ""),
            (run_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise DwaionWorkflowNotFound("The research run is unavailable.")
        return row

    def _record(self, row: Any) -> ResearchRun:
        progress = ResearchProgress.model_validate(self._decode(row, "progress"))
        result = ResearchResult.model_validate(self._decode(row, "result")) if row["result_envelope"] else None
        return ResearchRun(
            run_id=row["run_id"], plan_id=row["plan_id"], plan_revision=row["plan_revision"],
            state=row["run_state"], version=row["revision"], progress=progress,
            result=result, receipt_id=row["receipt_id"], safe_error_code=row["safe_error_code"],
            started_at=row["started_at"], created_at=row["created_at"],
            updated_at=row["updated_at"], completed_at=row["completed_at"],
        )

    def _payload(self, identity: PersonalDomainIdentity, run_id: UUID, field: str, payload: dict[str, object]) -> str:
        return self.codec.encrypt_json(
            payload, tenant_id=identity.tenant_id, resource_type="research-run",
            resource_id=str(run_id), field=field,
        )

    def _decode(self, row: Any, field: str) -> dict[str, object]:
        return self.codec.decrypt_json(
            row[f"{field}_envelope"], tenant_id=row["tenant_id"],
            resource_type="research-run", resource_id=str(row["run_id"]), field=field,
        )

    def _fingerprint(self, identity: PersonalDomainIdentity, operation: str, request: Any) -> str:
        return self.fingerprints.value(
            tenant_id=identity.tenant_id, purpose=f"research-run:{operation}",
            payload=request.model_dump(mode="json", by_alias=True),
        )

    @staticmethod
    def _event_replay(connection: Any, identity: PersonalDomainIdentity, command_id: UUID, fingerprint: str) -> bool:
        event = connection.execute(
            "SELECT request_fingerprint FROM ai_research_run_events WHERE tenant_id = %s AND user_id = %s AND command_id = %s",
            (identity.tenant_id, identity.user_id, command_id),
        ).fetchone()
        if event is None:
            return False
        if event["request_fingerprint"] != fingerprint:
            raise DwaionWorkflowConflict("The research command ID is already in use.")
        return True

    def _event(self, connection: Any, identity: PersonalDomainIdentity, row: Any, command_id: UUID, event_type: str, previous: str | None, fingerprint: str, detail: dict[str, object]) -> None:
        detail_envelope = self._payload(identity, row["run_id"], f"event-{command_id}", detail)
        connection.execute(
            """INSERT INTO ai_research_run_events (
                   event_id, run_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint, detail_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(), row["run_id"], identity.tenant_id, identity.user_id,
                identity.user_id, identity.correlation_id, command_id, event_type,
                previous, row["run_state"], row["revision"], fingerprint, detail_envelope,
            ),
        )


def research_worker_configured() -> bool:
    return os.getenv("DWP_DEEP_RESEARCH_WORKER_ENABLED", "false").strip().lower() == "true"


def _command_target(current: ResearchRunState, action: ResearchRunCommandAction) -> ResearchRunState:
    transitions = {
        ResearchRunCommandAction.PAUSE: ({ResearchRunState.QUEUED, ResearchRunState.RUNNING}, ResearchRunState.PAUSED),
        ResearchRunCommandAction.RESUME: ({ResearchRunState.PAUSED, ResearchRunState.PARTIAL, ResearchRunState.CONFLICT}, ResearchRunState.QUEUED),
        ResearchRunCommandAction.EXCLUDE_SOURCE_AND_CONTINUE: ({ResearchRunState.PARTIAL, ResearchRunState.CONFLICT}, ResearchRunState.QUEUED),
        ResearchRunCommandAction.REPROBE_SOURCE: ({ResearchRunState.PARTIAL, ResearchRunState.CONFLICT}, ResearchRunState.QUEUED),
        ResearchRunCommandAction.SAFE_CANCEL: ({ResearchRunState.QUEUED, ResearchRunState.RUNNING, ResearchRunState.PAUSED, ResearchRunState.PARTIAL, ResearchRunState.CONFLICT}, ResearchRunState.CANCELLING),
        ResearchRunCommandAction.EXTEND: ({ResearchRunState.QUEUED, ResearchRunState.RUNNING, ResearchRunState.PAUSED, ResearchRunState.PARTIAL}, current),
    }
    allowed, target = transitions[action]
    if current not in allowed:
        raise DwaionWorkflowConflict("The research run command is not allowed in its current state.")
    return target


def _worker_transition(current: ResearchRunState, target: ResearchRunState) -> None:
    allowed = {
        ResearchRunState.QUEUED: {ResearchRunState.RUNNING, ResearchRunState.PARTIAL, ResearchRunState.FAILED, ResearchRunState.COMPLETED, ResearchRunState.CANCELLED},
        ResearchRunState.RUNNING: {ResearchRunState.RUNNING, ResearchRunState.PARTIAL, ResearchRunState.CONFLICT, ResearchRunState.FAILED, ResearchRunState.COMPLETED, ResearchRunState.CANCELLED},
        ResearchRunState.PARTIAL: {ResearchRunState.RUNNING, ResearchRunState.PARTIAL, ResearchRunState.FAILED, ResearchRunState.COMPLETED, ResearchRunState.CANCELLED},
        ResearchRunState.CANCELLING: {ResearchRunState.CANCELLED, ResearchRunState.PARTIAL, ResearchRunState.FAILED},
    }
    if target not in allowed.get(current, set()):
        raise DwaionWorkflowConflict("The worker research state transition is not allowed.")


@lru_cache(maxsize=1)
def get_research_run_store() -> ResearchRunStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise DwaionWorkflowUnavailable("Research run storage is unavailable.")
    return ResearchRunStore(database_url)


_SELECT = "SELECT r.* FROM ai_research_runs r"
