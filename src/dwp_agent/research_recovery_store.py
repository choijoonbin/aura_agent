from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import (
    ResearchPlanDefinition,
    ResearchResult,
    ResearchRunState,
)
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec, advisory_lock
from .personal_domain_security import PersonalDomainIdentity
from .research_recovery_contracts import (
    ResearchRecoveryAction,
    ResearchRecoveryCommandRequest,
    ResearchRecoveryReceipt,
    ResearchSensitivityAssessment,
)
from .research_recovery_logic import (
    assess_research_sensitivity,
    merge_research_definitions,
)


class ResearchRecoveryStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise DwaionWorkflowUnavailable(
                "Research recovery security is unavailable."
            ) from error

    def execute(
        self,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        request: ResearchRecoveryCommandRequest,
    ) -> ResearchRecoveryReceipt:
        request_payload = request.model_dump(mode="json", by_alias=True)
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="research-recovery:command",
            payload={"runId": str(run_id), "request": request_payload},
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "research-recovery",
                    identity.tenant_id,
                    identity.user_id,
                    request.command_id,
                )
                replay = self._replay(
                    connection, identity, run_id, request, fingerprint
                )
                if replay is not None:
                    return replay
                run = connection.execute(
                    """SELECT * FROM ai_research_runs
                        WHERE run_id = %s AND tenant_id = %s AND user_id = %s
                        FOR UPDATE""",
                    (run_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if run is None:
                    raise DwaionWorkflowNotFound("The research run is unavailable.")
                if int(run["revision"]) != request.expected_version:
                    raise DwaionWorkflowConflict(
                        "The research run version has changed. Refresh before retrying."
                    )
                plan = connection.execute(
                    """SELECT * FROM ai_research_plans
                        WHERE plan_id = %s AND tenant_id = %s AND user_id = %s
                        FOR UPDATE""",
                    (run["plan_id"], identity.tenant_id, identity.user_id),
                ).fetchone()
                if plan is None:
                    raise DwaionWorkflowNotFound("The research plan is unavailable.")
                server_definition = self._definition(plan)

                target_plan_id: UUID | None = None
                target_plan_revision: int | None = None
                cached_run_id: UUID | None = None
                result_sha256: str | None = None
                sensitivity: ResearchSensitivityAssessment | None = None

                if request.action in {
                    ResearchRecoveryAction.SAVE_AS_FORK,
                    ResearchRecoveryAction.PULL_AND_MERGE,
                    ResearchRecoveryAction.KEEP_LOCAL,
                }:
                    definition = self._resolved_definition(
                        request, server_definition
                    )
                    target_plan_id, target_plan_revision = self._create_plan_fork(
                        connection, identity, run_id, request, fingerprint, definition
                    )
                elif request.action == ResearchRecoveryAction.RECALCULATE_SENSITIVITY:
                    if run["run_state"] != ResearchRunState.COMPLETED.value:
                        raise DwaionWorkflowConflict(
                            "Sensitivity can only be recalculated for a completed result."
                        )
                    result = self._result(run)
                    sensitivity = assess_research_sensitivity(result)
                    result_sha256 = result.result_sha256
                elif request.action == ResearchRecoveryAction.USE_CACHE_FALLBACK:
                    cached = self._cached_run(connection, identity, run)
                    cached_result = self._result(cached)
                    cached_run_id = cached["run_id"]
                    result_sha256 = cached_result.result_sha256
                else:  # pragma: no cover - enum validation keeps this unreachable
                    raise DwaionWorkflowConflict("The recovery action is unsupported.")

                receipt_id = uuid4()
                completed_at = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                receipt_payload: dict[str, object] = {
                    "receiptId": str(receipt_id),
                    "commandId": str(request.command_id),
                    "action": request.action.value,
                    "state": "COMPLETED",
                    "runId": str(run_id),
                    "sourcePlanId": str(plan["plan_id"]),
                    "sourcePlanRevision": int(plan["revision"]),
                    "targetPlanId": str(target_plan_id) if target_plan_id else None,
                    "targetPlanRevision": target_plan_revision,
                    "cachedRunId": str(cached_run_id) if cached_run_id else None,
                    "resultSha256": result_sha256,
                    "sensitivity": (
                        sensitivity.model_dump(mode="json", by_alias=True)
                        if sensitivity
                        else None
                    ),
                    "completedAt": completed_at.isoformat(),
                }
                integrity = self.fingerprints.value(
                    tenant_id=identity.tenant_id,
                    purpose="research-recovery:receipt",
                    payload=receipt_payload,
                )
                receipt = ResearchRecoveryReceipt.model_validate(
                    {**receipt_payload, "integrityFingerprint": integrity}
                )

                if cached_run_id is not None:
                    cached_result_envelope = self.codec.encrypt_json(
                        cached_result.model_dump(mode="json", by_alias=True),
                        tenant_id=identity.tenant_id,
                        resource_type="research-run",
                        resource_id=str(run_id),
                        field="result",
                    )
                    updated = connection.execute(
                        """UPDATE ai_research_runs
                              SET run_state = 'COMPLETED', revision = revision + 1,
                                  generation = generation + 1,
                                  result_envelope = %s, receipt_id = %s,
                                  safe_error_code = NULL, completed_at = %s,
                                  execution_authorization_envelope = NULL,
                                  lease_token = NULL, lease_expires_at = NULL,
                                  lease_owner = NULL,
                                  updated_at = %s
                            WHERE run_id = %s
                        RETURNING *""",
                        (
                            cached_result_envelope,
                            receipt_id,
                            completed_at,
                            completed_at,
                            run_id,
                        ),
                    ).fetchone()
                    self._run_event(
                        connection,
                        identity,
                        updated,
                        request.command_id,
                        run["run_state"],
                        fingerprint,
                        cached_run_id,
                    )

                request_envelope = self.codec.encrypt_json(
                    request_payload,
                    tenant_id=identity.tenant_id,
                    resource_type="research-recovery",
                    resource_id=str(receipt_id),
                    field="request",
                )
                receipt_envelope = self.codec.encrypt_json(
                    receipt.model_dump(mode="json", by_alias=True),
                    tenant_id=identity.tenant_id,
                    resource_type="research-recovery",
                    resource_id=str(receipt_id),
                    field="receipt",
                )
                connection.execute(
                    """INSERT INTO ai_research_recovery_commands (
                           receipt_id, run_id, tenant_id, user_id, command_id,
                           idempotency_key, recovery_action, request_fingerprint,
                           request_envelope, receipt_envelope, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        receipt_id,
                        run_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.command_id,
                        request.idempotency_key,
                        request.action.value,
                        fingerprint,
                        request_envelope,
                        receipt_envelope,
                        completed_at,
                    ),
                )
                connection.execute(
                    """INSERT INTO ai_research_recovery_events (
                           event_id, receipt_id, run_id, tenant_id, user_id,
                           actor_user_id, correlation_id, command_id,
                           recovery_action, event_type, occurred_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'COMPLETED', %s)""",
                    (
                        uuid4(),
                        receipt_id,
                        run_id,
                        identity.tenant_id,
                        identity.user_id,
                        identity.user_id,
                        identity.correlation_id,
                        request.command_id,
                        request.action.value,
                        completed_at,
                    ),
                )
                return receipt
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable(
                "Research recovery storage is unavailable."
            ) from error

    def _replay(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        request: ResearchRecoveryCommandRequest,
        fingerprint: str,
    ) -> ResearchRecoveryReceipt | None:
        row = connection.execute(
            """SELECT * FROM ai_research_recovery_commands
                WHERE tenant_id = %s AND user_id = %s
                  AND (command_id = %s OR idempotency_key = %s)""",
            (
                identity.tenant_id,
                identity.user_id,
                request.command_id,
                request.idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        if row["run_id"] != run_id or row["request_fingerprint"] != fingerprint:
            raise DwaionWorkflowConflict(
                "The research recovery command identity is already in use."
            )
        payload = self.codec.decrypt_json(
            row["receipt_envelope"],
            tenant_id=identity.tenant_id,
            resource_type="research-recovery",
            resource_id=str(row["receipt_id"]),
            field="receipt",
        )
        return ResearchRecoveryReceipt.model_validate(payload)

    def _create_plan_fork(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        request: ResearchRecoveryCommandRequest,
        fingerprint: str,
        definition: ResearchPlanDefinition,
    ) -> tuple[UUID, int]:
        plan_id = uuid4()
        command_id = uuid5(
            NAMESPACE_URL,
            f"urn:dwp:research-recovery:{request.command_id}:plan-fork",
        )
        definition_payload = definition.model_dump(mode="json", by_alias=True)
        definition_envelope = self.codec.encrypt_json(
            definition_payload,
            tenant_id=identity.tenant_id,
            resource_type="research-plan",
            resource_id=str(plan_id),
            field="definition",
        )
        definition_fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="research-plan-definition",
            payload=definition_payload,
        )
        connection.execute(
            """INSERT INTO ai_research_plans (
                   plan_id, tenant_id, user_id, command_id, plan_state,
                   definition_envelope, definition_fingerprint)
               VALUES (%s, %s, %s, %s, 'READY', %s, %s)""",
            (
                plan_id,
                identity.tenant_id,
                identity.user_id,
                command_id,
                definition_envelope,
                definition_fingerprint,
            ),
        )
        connection.execute(
            """INSERT INTO ai_research_plan_commands (
                   tenant_id, user_id, command_id, plan_id, command_type,
                   request_fingerprint)
               VALUES (%s, %s, %s, %s, 'CREATE', %s)""",
            (
                identity.tenant_id,
                identity.user_id,
                command_id,
                plan_id,
                fingerprint,
            ),
        )
        return plan_id, 1

    @staticmethod
    def _resolved_definition(
        request: ResearchRecoveryCommandRequest,
        server: ResearchPlanDefinition,
    ) -> ResearchPlanDefinition:
        if request.action == ResearchRecoveryAction.SAVE_AS_FORK:
            return server
        if request.action == ResearchRecoveryAction.KEEP_LOCAL:
            assert request.local_definition is not None
            return request.local_definition
        assert request.local_definition is not None
        return merge_research_definitions(server, request.local_definition)

    def _definition(self, row: Any) -> ResearchPlanDefinition:
        payload = self.codec.decrypt_json(
            row["definition_envelope"],
            tenant_id=row["tenant_id"],
            resource_type="research-plan",
            resource_id=str(row["plan_id"]),
            field="definition",
        )
        return ResearchPlanDefinition.model_validate(payload)

    def _result(self, row: Any) -> ResearchResult:
        if row["result_envelope"] is None:
            raise DwaionWorkflowConflict("The research result evidence is unavailable.")
        payload = self.codec.decrypt_json(
            row["result_envelope"],
            tenant_id=row["tenant_id"],
            resource_type="research-run",
            resource_id=str(row["run_id"]),
            field="result",
        )
        return ResearchResult.model_validate(payload)

    @staticmethod
    def _cached_run(connection: Any, identity: PersonalDomainIdentity, run: Any) -> Any:
        if run["run_state"] not in {
            ResearchRunState.PARTIAL.value,
            ResearchRunState.CONFLICT.value,
            ResearchRunState.FAILED.value,
        }:
            raise DwaionWorkflowConflict(
                "Cache fallback is only available for an incomplete research run."
            )
        cached = connection.execute(
            """SELECT * FROM ai_research_runs
                WHERE plan_id = %s AND tenant_id = %s AND user_id = %s
                  AND run_id <> %s AND run_state = 'COMPLETED'
                  AND result_envelope IS NOT NULL AND receipt_id IS NOT NULL
                ORDER BY completed_at DESC, run_id DESC
                LIMIT 1""",
            (
                run["plan_id"],
                identity.tenant_id,
                identity.user_id,
                run["run_id"],
            ),
        ).fetchone()
        if cached is None:
            raise DwaionWorkflowConflict(
                "No verified completed result is available in the governed cache."
            )
        return cached

    def _run_event(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        row: Any,
        command_id: UUID,
        previous_state: str,
        request_fingerprint: str,
        cached_run_id: UUID,
    ) -> None:
        detail_envelope = self.codec.encrypt_json(
            {
                "action": ResearchRecoveryAction.USE_CACHE_FALLBACK.value,
                "cachedRunId": str(cached_run_id),
            },
            tenant_id=identity.tenant_id,
            resource_type="research-run",
            resource_id=str(row["run_id"]),
            field=f"event-{command_id}",
        )
        connection.execute(
            """INSERT INTO ai_research_run_events (
                   event_id, run_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint, detail_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s,
                       'CACHE_FALLBACK_COMPLETED', %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                row["run_id"],
                identity.tenant_id,
                identity.user_id,
                identity.user_id,
                identity.correlation_id,
                command_id,
                previous_state,
                row["run_state"],
                row["revision"],
                request_fingerprint,
                detail_envelope,
            ),
        )


@lru_cache(maxsize=1)
def get_research_recovery_store() -> ResearchRecoveryStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise DwaionWorkflowUnavailable("Research recovery storage is unavailable.")
    return ResearchRecoveryStore(database_url)
