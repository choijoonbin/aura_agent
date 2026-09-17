from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import (
    ExecuteResearchRunRequest,
    ResearchProgress,
    ResearchResult,
    ResearchRun,
    ResearchRunCommandAction,
    ResearchRunCommandRequest,
    ResearchRunState,
    ResearchWorkerObservation,
    StartResearchRunRequest,
)
from .dwaion_workflow_errors import DwaionWorkflowConflict, DwaionWorkflowNotFound, DwaionWorkflowUnavailable
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec, advisory_lock
from .governed_worker_runtime import governed_worker_available
from .personal_domain_security import PersonalDomainIdentity
from .research_plan_store import ResearchPlanStore, get_research_plan_store
from .research_run_runtime import ResearchExecutionAuthorization, ResearchRuntimeControls
from .workspace_authorization import WorkspaceRequestAuthorization

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
                controls_envelope = self._payload(
                    identity,
                    run_id,
                    "runtime-controls",
                    ResearchRuntimeControls().model_dump(mode="json", by_alias=True),
                )
                row = connection.execute(
                    """INSERT INTO ai_research_runs (
                           run_id, plan_id, tenant_id, user_id, command_id,
                           idempotency_key, run_state, plan_revision,
                           progress_envelope, runtime_controls_envelope,
                           safe_error_code)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *""",
                    (
                        run_id, plan_id, identity.tenant_id, identity.user_id,
                        request.command_id, request.idempotency_key, state.value,
                        plan.revision, progress_envelope, controls_envelope,
                        None if state == ResearchRunState.QUEUED else "RESEARCH_WORKER_NOT_CONFIGURED",
                    ),
                ).fetchone()
                self._event(connection, identity, row, request.command_id, "START_REQUESTED", None, fingerprint, {"workerConfigured": research_worker_configured()})
                return self._record(row)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research run storage is unavailable.") from error

    def request_execution(
        self,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        request: ExecuteResearchRunRequest,
        workspace_authorization: WorkspaceRequestAuthorization,
    ) -> ResearchRun:
        from .research_run_execution_queue import queue_research_execution

        return queue_research_execution(
            self, identity, run_id, request, workspace_authorization
        )

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
        workspace_authorization: WorkspaceRequestAuthorization | None = None,
    ) -> ResearchRun:
        execution_authorization = None
        if workspace_authorization is not None and request.action in {
            ResearchRunCommandAction.RESUME,
            ResearchRunCommandAction.EXCLUDE_SOURCE_AND_CONTINUE,
            ResearchRunCommandAction.REPROBE_SOURCE,
        }:
            try:
                execution_authorization = ResearchExecutionAuthorization.capture(
                    identity, workspace_authorization
                )
            except ValueError as error:
                raise DwaionWorkflowConflict(str(error)) from error
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
                controls = self._runtime_controls(row)
                source_key = request.source_key
                if source_key is not None:
                    plan = self.plan_store.get(identity, row["plan_id"])
                    allowed_sources = {
                        policy.source_key
                        for policy in plan.definition.source_policies
                        if policy.allowed
                    }
                    if (
                        plan.revision != int(row["plan_revision"])
                        or source_key not in allowed_sources
                    ):
                        raise DwaionWorkflowConflict(
                            "The research source is not allowed by the pinned plan."
                        )
                if request.action == ResearchRunCommandAction.EXTEND:
                    progress["recoveryHint"] = f"Budget extended by {request.extension_minutes} minutes."
                    extension = request.extension_minutes or 0
                    if controls.extension_minutes + extension > 1_440:
                        raise DwaionWorkflowConflict(
                            "The cumulative research extension limit has been reached."
                        )
                    base_deadline = controls.deadline_at or datetime.now(UTC)
                    controls = ResearchRuntimeControls.model_validate(
                        {
                            **controls.model_dump(mode="json"),
                            "deadline_at": max(base_deadline, datetime.now(UTC))
                            + timedelta(minutes=extension),
                            "extension_minutes": controls.extension_minutes + extension,
                        }
                    )
                elif request.action in {
                    ResearchRunCommandAction.EXCLUDE_SOURCE_AND_CONTINUE,
                    ResearchRunCommandAction.REPROBE_SOURCE,
                }:
                    progress["recoveryHint"] = f"{request.action.value}: {request.source_key}"
                    excluded = set(controls.excluded_source_keys)
                    reprobed = set(controls.reprobe_source_keys)
                    if request.action == ResearchRunCommandAction.EXCLUDE_SOURCE_AND_CONTINUE:
                        excluded.add(source_key)
                        reprobed.discard(source_key)
                    else:
                        excluded.discard(source_key)
                        reprobed.add(source_key)
                    controls = ResearchRuntimeControls.model_validate(
                        {
                            **controls.model_dump(mode="json"),
                            "excluded_source_keys": sorted(excluded),
                            "reprobe_source_keys": sorted(reprobed),
                        }
                    )
                if target == ResearchRunState.QUEUED and not research_worker_configured():
                    target = ResearchRunState.PARTIAL
                    progress["recoveryHint"] = "Deep Research worker is not configured."
                invalidates_lease = request.action != ResearchRunCommandAction.EXTEND
                authorization_envelope = (
                    self._payload(
                        identity,
                        run_id,
                        "execution-authorization",
                        execution_authorization.model_dump(mode="json", by_alias=True),
                    )
                    if execution_authorization is not None
                    else None
                )
                clears_authorization = (
                    request.action == ResearchRunCommandAction.SAFE_CANCEL
                )
                safe_error_code = (
                    row["safe_error_code"]
                    if request.action == ResearchRunCommandAction.EXTEND
                    else "RESEARCH_WORKER_NOT_CONFIGURED"
                    if target == ResearchRunState.PARTIAL and not research_worker_configured()
                    else None
                )
                updated = connection.execute(
                    """UPDATE ai_research_runs
                          SET run_state = %s, revision = revision + 1,
                              progress_envelope = %s,
                              runtime_controls_envelope = %s,
                              runtime_deadline_at = %s,
                              generation = generation + %s,
                              lease_token = CASE WHEN %s THEN NULL ELSE lease_token END,
                              lease_expires_at = CASE WHEN %s THEN NULL ELSE lease_expires_at END,
                              lease_owner = CASE WHEN %s THEN NULL ELSE lease_owner END,
                              execution_authorization_envelope = CASE
                                  WHEN %s THEN NULL
                                  ELSE COALESCE(%s, execution_authorization_envelope)
                              END,
                              safe_error_code = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = %s
                    RETURNING *""",
                    (
                        target.value,
                        self._payload(identity, run_id, "progress", progress),
                        self._payload(
                            identity,
                            run_id,
                            "runtime-controls",
                            controls.model_dump(mode="json", by_alias=True),
                        ),
                        controls.deadline_at,
                        1 if invalidates_lease else 0,
                        invalidates_lease,
                        invalidates_lease,
                        invalidates_lease,
                        clears_authorization,
                        authorization_envelope,
                        safe_error_code,
                        run_id,
                    ),
                ).fetchone()
                self._event(
                    connection,
                    identity,
                    updated,
                    request.command_id,
                    request.action.value,
                    current.value,
                    fingerprint,
                    {
                        "reason": request.reason,
                        "sourceKey": request.source_key,
                        "extensionMinutes": request.extension_minutes,
                    },
                )
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
                if (
                    row.get("execution_authorization_envelope") is not None
                    or int(row.get("generation") or 0) > 0
                ):
                    raise DwaionWorkflowConflict(
                        "Leased research execution cannot accept an unfenced observation."
                    )
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

    def _runtime_controls(self, row: Any) -> ResearchRuntimeControls:
        envelope = row.get("runtime_controls_envelope")
        if envelope is None:
            return ResearchRuntimeControls()
        payload = self.codec.decrypt_json(
            envelope,
            tenant_id=row["tenant_id"],
            resource_type="research-run",
            resource_id=str(row["run_id"]),
            field="runtime-controls",
        )
        return ResearchRuntimeControls.model_validate(payload)

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
    enabled = os.getenv("DWP_DEEP_RESEARCH_WORKER_ENABLED", "false").strip().lower()
    return enabled == "true" and governed_worker_available("RESEARCH_RUN")

def _command_target(current: ResearchRunState, action: ResearchRunCommandAction) -> ResearchRunState:
    transitions = {
        ResearchRunCommandAction.PAUSE: ({ResearchRunState.QUEUED, ResearchRunState.RUNNING}, ResearchRunState.PAUSED),
        ResearchRunCommandAction.RESUME: ({ResearchRunState.PAUSED, ResearchRunState.PARTIAL, ResearchRunState.CONFLICT}, ResearchRunState.QUEUED),
        ResearchRunCommandAction.EXCLUDE_SOURCE_AND_CONTINUE: ({ResearchRunState.RUNNING, ResearchRunState.PAUSED, ResearchRunState.PARTIAL, ResearchRunState.CONFLICT}, ResearchRunState.QUEUED),
        ResearchRunCommandAction.REPROBE_SOURCE: ({ResearchRunState.RUNNING, ResearchRunState.PAUSED, ResearchRunState.PARTIAL, ResearchRunState.CONFLICT}, ResearchRunState.QUEUED),
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
