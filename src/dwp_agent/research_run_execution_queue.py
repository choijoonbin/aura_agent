from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import ExecuteResearchRunRequest, ResearchRun, ResearchRunState
from .dwaion_workflow_errors import DwaionWorkflowConflict, DwaionWorkflowNotFound, DwaionWorkflowUnavailable
from .personal_domain_security import PersonalDomainIdentity
from .research_run_runtime import ResearchExecutionAuthorization, ResearchRuntimeControls
from .workspace_authorization import WorkspaceRequestAuthorization

if TYPE_CHECKING:
    from .research_run_store import ResearchRunStore


def queue_research_execution(
    store: "ResearchRunStore",
    identity: PersonalDomainIdentity,
    run_id: UUID,
    request: ExecuteResearchRunRequest,
    workspace_authorization: WorkspaceRequestAuthorization,
) -> ResearchRun:
    from .research_run_store import research_worker_configured

    if not research_worker_configured():
        raise DwaionWorkflowConflict(
            "The governed Deep Research worker is not configured."
        )
    try:
        authorization = ResearchExecutionAuthorization.capture(
            identity, workspace_authorization
        )
    except ValueError as error:
        raise DwaionWorkflowConflict(str(error)) from error
    fingerprint = store._fingerprint(identity, "execute", request)
    try:
        with connect(store.database_url, row_factory=dict_row) as connection:
            row = store._locked(connection, identity, run_id)
            if store._event_replay(connection, identity, request.command_id, fingerprint):
                return store._record(row)
            if int(row["revision"]) != request.expected_version:
                raise DwaionWorkflowConflict("The research run version has changed.")
            if ResearchRunState(row["run_state"]) not in {
                ResearchRunState.QUEUED,
                ResearchRunState.PARTIAL,
            }:
                raise DwaionWorkflowConflict(
                    "The research run cannot be queued from its current state."
                )
            plan = store.plan_store.get(identity, row["plan_id"])
            if plan.revision != int(row["plan_revision"]):
                raise DwaionWorkflowConflict(
                    "The research run is bound to a different plan revision."
                )
            deadline = datetime.now(UTC) + timedelta(
                minutes=plan.definition.budget.maximum_minutes
            )
            controls = ResearchRuntimeControls(deadline_at=deadline)
            updated = connection.execute(
                """UPDATE ai_research_runs
                      SET run_state = 'QUEUED', revision = revision + 1,
                          generation = generation + 1,
                          runtime_controls_envelope = %s,
                          execution_authorization_envelope = %s,
                          execution_command_id = %s,
                          execution_requested_at = CURRENT_TIMESTAMP,
                          runtime_deadline_at = %s,
                          lease_token = NULL, lease_expires_at = NULL,
                          lease_owner = NULL, safe_error_code = NULL,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s
                RETURNING *""",
                (
                    store._payload(
                        identity, run_id, "runtime-controls",
                        controls.model_dump(mode="json", by_alias=True),
                    ),
                    store._payload(
                        identity, run_id, "execution-authorization",
                        authorization.model_dump(mode="json", by_alias=True),
                    ),
                    request.command_id,
                    deadline,
                    run_id,
                ),
            ).fetchone()
            store._event(
                connection, identity, updated, request.command_id,
                "EXECUTION_QUEUED", row["run_state"], fingerprint,
                {"deadlineAt": deadline.isoformat()},
            )
            return store._record(updated)
    except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
        raise
    except (PsycopgError, ValueError, TypeError) as error:
        raise DwaionWorkflowUnavailable("Research run storage is unavailable.") from error
