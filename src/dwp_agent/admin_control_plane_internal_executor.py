from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from psycopg import connect
from psycopg.rows import dict_row

from .admin_control_plane_adapters import (
    AdminCommandExecutionContext,
    AdminCommandExecutionRejected,
    AdminCommandExecutionResult,
)
from .admin_control_plane_contracts import (
    EvaluationDatasetSummary,
    GovernedCommandKind,
    GovernedCommandState,
    ImprovementBacklogItem,
    IncidentSummary,
    TokenBudgetSummary,
)
from .admin_control_plane_registry import AdminCommandResourceStrategy, AdminCommandSpec
from .admin_model_routing_contracts import ModelRouteSimulationInput
from .ai_control_contracts import TenantAIExecutionPolicy
from .ai_control_activation import ai_runtime_control_activation_state
from .ai_control_store import PostgresAIControlStore
from .governed_domain_core import GovernedPayloadCodec


class AdminInternalCommandExecutor:
    def __init__(self, database_url: str, *, worker_id: str) -> None:
        self.database_url = database_url
        self.worker_id = worker_id
        self.codec = GovernedPayloadCodec()

    def execute(
        self, context: AdminCommandExecutionContext, spec: AdminCommandSpec
    ) -> AdminCommandExecutionResult:
        if context.rollback_requested:
            return self._rollback(context, spec)
        if spec.operation == "MODEL_ROUTE_SIMULATE":
            snapshot = self._model_route_simulation(context)
        elif spec.operation in {
            "MODEL_ROUTING_UPDATE",
            "EMERGENCY_STOP",
            "EMERGENCY_RECOVERY",
            "TOKEN_BUDGET",
        }:
            snapshot = self._ai_policy_result(context, spec.operation)
        else:
            snapshot = self._local_resource_result(context, spec)
        version = self._next_result_version(context, spec)
        if (
            context.kind != GovernedCommandKind.MODEL_ROUTE_SIMULATE
            and not (
                spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY
                and spec.result_resource_type is None
            )
        ):
            snapshot["version"] = version
        resource_type, resource_id = spec.output_resource(
            target_type=context.target_type,
            target_id=context.target_id,
            command_id=str(context.command_id),
        )
        return AdminCommandExecutionResult(
            state=GovernedCommandState.SUCCEEDED,
            summary=f"{context.kind.value} completed against authoritative resource version {version}.",
            domain_receipt_ref=(
                f"admin-resource:{context.tenant_id}:{resource_type}:{resource_id}:"
                f"v{version}:command:{context.command_id}"
            ),
            snapshot=snapshot,
            version=version,
        )

    def _local_resource_result(
        self, context: AdminCommandExecutionContext, spec: AdminCommandSpec
    ) -> dict[str, object]:
        current, _ = self._resource_snapshot(context.target_type, context.target_id, context.tenant_id)
        now = datetime.now(UTC).isoformat()
        value: dict[str, object] = dict(current or {})
        if spec.operation == "BACKLOG":
            self._require_target(context, "IMPROVEMENT_BACKLOG")
            value.update(context.payload)
            value.update({
                "itemId": context.target_id,
                "ownerTeam": context.payload.get("ownerTeam", value.get("ownerTeam", "UNASSIGNED")),
                "version": context.expected_target_version + 1,
            })
            return ImprovementBacklogItem.model_validate(value).model_dump(
                mode="json", by_alias=True
            )
        elif spec.operation == "BACKLOG_RELEASE_LINK":
            self._require_target(context, "IMPROVEMENT_BACKLOG")
            value["linkedRelease"] = context.payload.get("linkedRelease")
            return ImprovementBacklogItem.model_validate(value).model_dump(
                mode="json", by_alias=True
            )
        elif spec.operation == "TOKEN_BUDGET":
            self._require_target(context, "TOKEN_BUDGET")
            value.update({
                key: context.payload[key]
                for key in ("budgetTokens", "policyMode")
                if key in context.payload
            })
            value.update({
                "scope": context.target_id,
                "consumedTokens": int(value.get("consumedTokens", 0)),
                "projectedTokens": value.get("projectedTokens"),
                "spikeDetected": bool(value.get("spikeDetected", False)),
                "enforcementActivationState": ai_runtime_control_activation_state().value,
                "version": context.expected_target_version + 1,
            })
            return TokenBudgetSummary.model_validate(value).model_dump(
                mode="json", by_alias=True
            )
        elif spec.operation == "DATASET_PII_DECIDE":
            self._require_target(context, "EVALUATION_DATASET")
            decision = str(context.payload.get("decision", "REVIEW")).upper()
            value["piiState"] = {"APPROVE": "PASS", "APPROVED": "PASS"}.get(decision, decision)
            value["updatedAt"] = now
            value["version"] = context.expected_target_version + 1
            return EvaluationDatasetSummary.model_validate(value).model_dump(
                mode="json", by_alias=True
            )
        elif spec.operation == "INCIDENT_CLOSE":
            self._require_target(context, "AI_INCIDENT")
            timeline = list(value.get("timeline", [])) if isinstance(value.get("timeline"), list) else []
            timeline.append({
                "eventId": str(uuid5(NAMESPACE_URL, f"dwp-admin-incident-close:{context.command_id}")),
                "type": "INCIDENT_CLOSED",
                "summary": str(context.payload.get("resolutionSummary", "Incident closed by governed command.")),
                "actorRef": self.worker_id,
                "occurredAt": now,
                "evidenceRefs": list(context.review.get("evidenceRefs", [])),
            })
            value.update({"state": "CLOSED", "timeline": timeline, "updatedAt": now})
            return IncidentSummary.model_validate(value).model_dump(
                mode="json", by_alias=True
            )
        elif spec.operation == "EVIDENCE_ATTACH":
            value.update(context.payload)
            value.update({
                "resourceKind": "DRIFT_EVIDENCE",
                "sourceSignalId": context.payload.get("signalId", context.target_id),
                "evidenceRefs": list(context.review.get("evidenceRefs", [])),
                "attachedAt": now,
            })
        elif spec.operation == "COST_SIMULATE":
            value.update(context.payload)
            workload = _nonnegative_number(context.payload.get("workloadVolume"))
            input_tokens = _nonnegative_number(context.payload.get("averageInputTokens"))
            output_tokens = _nonnegative_number(context.payload.get("averageOutputTokens"))
            value.update({
                "resourceKind": "COST_SIMULATION",
                "simulationId": str(context.command_id),
                "estimatedTokens": int(workload * (input_tokens + output_tokens)),
                "productionPolicyChanged": False,
                "generatedAt": now,
            })
        elif spec.operation == "SIMULATION":
            value.update(context.payload)
            value.update({
                "resourceKind": spec.result_resource_type or context.target_type,
                "simulationId": str(context.command_id),
                "productionPolicyChanged": False,
                "generatedAt": now,
            })
        else:
            value.update(context.payload)
            value.update({
                "resourceKind": context.target_type,
                "resourceId": context.target_id,
                "commandKind": context.kind.value,
                "updatedAt": now,
            })
        return value

    @staticmethod
    def _require_target(
        context: AdminCommandExecutionContext, expected_type: str
    ) -> None:
        if context.target_type != expected_type:
            raise AdminCommandExecutionRejected(
                "ADMIN_TARGET_TYPE_INVALID",
                f"{context.kind.value} requires target type {expected_type}.",
                "Reload the authoritative admin resource and submit the command against its exact target type.",
            )

    def _model_route_simulation(self, context: AdminCommandExecutionContext) -> dict[str, object]:
        ModelRouteSimulationInput.model_validate(context.payload)
        try:
            policy = PostgresAIControlStore(self.database_url).policy(tenant_id=str(context.tenant_id))
        except Exception as error:
            raise AdminCommandExecutionRejected(
                "AI_ROUTING_POLICY_NOT_CONFIGURED",
                "A tenant AI routing policy is required for route simulation.",
                "Configure a governed AI execution policy and retry the read-only simulation.",
            ) from error
        routes = policy.allowed_model_routes
        first = routes[0]
        model_ids = [f"{route.provider}:{route.model}" for route in routes]
        decision = "BLOCKED" if policy.emergency_disabled else "ROUTED"
        if not policy.emergency_disabled and first.availability_state.value != "VERIFIED":
            decision = "REVIEW_REQUIRED"
        return {
            "simulationId": str(context.command_id),
            "decision": decision,
            "matchedRuleId": "ask-runtime-1",
            "targetModelId": None if decision == "BLOCKED" else model_ids[0],
            "estimatedCost": None,
            "currency": os.getenv("DWP_OUTCOME_CURRENCY", "USD"),
            "estimatedLatencyMs": None,
            "fallbackModelIds": model_ids[1:],
            "generatedAt": datetime.now(UTC).isoformat(),
        }

    def _ai_policy_result(
        self, context: AdminCommandExecutionContext, operation: str
    ) -> dict[str, object]:
        try:
            policy = PostgresAIControlStore(self.database_url).policy(tenant_id=str(context.tenant_id))
        except Exception as error:
            raise AdminCommandExecutionRejected(
                "AI_ROUTING_POLICY_NOT_CONFIGURED",
                "The tenant AI execution policy is not configured.",
                "Bootstrap the governed AI execution policy before running this command.",
            ) from error
        if policy.policy_version != context.expected_target_version:
            raise AdminCommandExecutionRejected(
                "ADMIN_TARGET_VERSION_STALE",
                "The tenant AI execution policy changed before command execution.",
                "Reload the current policy, review the diff and submit a new governed command.",
            )
        value = policy.model_dump(mode="json", by_alias=True)
        if operation == "MODEL_ROUTING_UPDATE":
            requested = [context.payload.get("primaryModelId")]
            fallbacks = context.payload.get("fallbackModelIds", [])
            if isinstance(fallbacks, list):
                requested.extend(fallbacks)
            routes = list(value["allowedModelRoutes"])
            by_id = {f"{route['provider']}:{route['model']}": route for route in routes}
            by_id.update({key.lower(): route for key, route in list(by_id.items())})
            ordered = [
                by_id.get(item, by_id.get(item.lower()))
                for item in requested
                if isinstance(item, str) and (item in by_id or item.lower() in by_id)
            ]
            ordered = [route for route in ordered if route is not None]
            ordered.extend(route for route in routes if route not in ordered)
            primary = requested[0] if requested else None
            if not ordered or not isinstance(primary, str) or (
                primary not in by_id and primary.lower() not in by_id
            ):
                raise AdminCommandExecutionRejected(
                    "MODEL_ROUTE_NOT_REGISTERED",
                    "The requested primary model is not present in the governed AI execution policy.",
                    "Register and validate the model route before retrying the policy update.",
                )
            value["allowedModelRoutes"] = ordered
        elif operation == "TOKEN_BUDGET":
            self._require_target(context, "TOKEN_BUDGET")
            if context.target_id != "ASK_RUNTIME":
                raise AdminCommandExecutionRejected(
                    "TOKEN_BUDGET_SCOPE_UNSUPPORTED",
                    "The runtime currently enforces token budgets at the ASK_RUNTIME tenant scope.",
                    "Select the ASK_RUNTIME budget returned by the authoritative outcomes snapshot.",
                )
            raw_budget = context.payload.get("budgetTokens")
            if isinstance(raw_budget, bool):
                raw_budget = None
            try:
                budget = int(raw_budget)  # type: ignore[arg-type]
            except (TypeError, ValueError) as error:
                raise AdminCommandExecutionRejected(
                    "TOKEN_BUDGET_INVALID",
                    "The token budget must be a whole positive number.",
                    "Enter a value between 1 and 10,000,000,000 tokens.",
                ) from error
            if budget < 1 or budget > 10_000_000_000:
                raise AdminCommandExecutionRejected(
                    "TOKEN_BUDGET_INVALID",
                    "The token budget is outside the enforced range.",
                    "Enter a value between 1 and 10,000,000,000 tokens.",
                )
            mode = str(context.payload.get("policyMode", "")).upper()
            runtime_modes = {
                "WARN": "ALERT_ONLY",
                "THROTTLE": "THROTTLED",
                "BLOCK": "ENFORCED",
            }
            if mode not in runtime_modes:
                raise AdminCommandExecutionRejected(
                    "TOKEN_BUDGET_MODE_INVALID",
                    "The requested token budget mode is unsupported.",
                    "Choose WARN, THROTTLE, or BLOCK.",
                )
            value["periodTokenLimit"] = budget
            value["budgetEnforcementMode"] = runtime_modes[mode]
        else:
            value["emergencyDisabled"] = operation == "EMERGENCY_STOP"
        value["policyVersion"] = context.expected_target_version + 1
        value["updatedAt"] = datetime.now(UTC).isoformat()
        TenantAIExecutionPolicy.model_validate(value)
        return value

    def _rollback(
        self, context: AdminCommandExecutionContext, spec: AdminCommandSpec
    ) -> AdminCommandExecutionResult:
        if (
            spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY
            and spec.result_resource_type is None
        ):
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """SELECT previous_value FROM ai_governance_events
                        WHERE tenant_id = %s AND category = 'AI_CONTROL'
                          AND target_key = %s AND previous_value IS NOT NULL
                        ORDER BY created_at DESC LIMIT 1""",
                    (context.tenant_id, str(context.command_id)),
                ).fetchone()
            if row is None:
                raise AdminCommandExecutionRejected(
                    "ADMIN_ROLLBACK_SNAPSHOT_MISSING",
                    "The previous AI execution policy snapshot is unavailable.",
                    "Keep the current policy isolated and restore it through a new reviewed policy command.",
                )
            policy = TenantAIExecutionPolicy.model_validate(row["previous_value"])
            snapshot = policy.model_dump(mode="json", by_alias=True)
            snapshot["policyVersion"] = context.expected_target_version + 1
            snapshot["updatedAt"] = datetime.now(UTC).isoformat()
        elif spec.result_resource_type is not None or spec.resource_strategy in {
            AdminCommandResourceStrategy.TARGET,
            AdminCommandResourceStrategy.CREATE_TARGET,
        }:
            resource_type, resource_id = spec.output_resource(
                target_type=context.target_type,
                target_id=context.target_id,
                command_id=str(context.command_id),
            )
            _, output_version = self._resource_snapshot(
                resource_type, resource_id, context.tenant_id
            )
            snapshot = self._previous_resource_snapshot(
                context, resource_type, resource_id, output_version
            )
        else:
            raise AdminCommandExecutionRejected(
                "ADMIN_ROLLBACK_REQUIRES_DOMAIN_ADAPTER",
                "This command cannot be rolled back by the local resource ledger.",
                "Configure the owning domain adapter and submit a governed rollback there.",
            )
        version = (
            output_version + 1
            if spec.result_resource_type is not None
            else context.expected_target_version + 1
        )
        if not (
            spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY
            and spec.result_resource_type is None
        ):
            snapshot["version"] = version
        source_ref = context.rollback_source_receipt_ref
        if not source_ref:
            raise AdminCommandExecutionRejected(
                "ADMIN_ROLLBACK_LINK_MISSING",
                "The rollback request is missing its source receipt binding.",
                "Reload the completed command receipt and submit a new rollback request.",
            )
        return AdminCommandExecutionResult(
            state=GovernedCommandState.ROLLED_BACK,
            summary=f"Restored the prior {context.target_type} state as version {version}.",
            domain_receipt_ref=f"admin-resource:rollback:{context.command_id}:v{version}",
            rollback_ref=source_ref,
            snapshot=snapshot,
            version=version,
        )

    def _previous_resource_snapshot(
        self,
        context: AdminCommandExecutionContext,
        resource_type: str,
        resource_id: str,
        current_version: int,
    ) -> dict[str, object]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT * FROM ai_admin_control_resource_versions
                    WHERE tenant_id = %s AND resource_type = %s AND resource_id = %s
                      AND resource_version < %s
                    ORDER BY resource_version DESC LIMIT 1""",
                (
                    context.tenant_id,
                    resource_type,
                    resource_id,
                    current_version,
                ),
            ).fetchone()
        if row is None:
            return {
                "resourceKind": resource_type,
                "resourceId": resource_id,
                "rolledBack": True,
                "tombstoned": True,
            }
        snapshot = self.codec.decrypt_json(
            row["snapshot_envelope"],
            tenant_id=context.tenant_id,
            resource_type="admin-control-resource",
            resource_id=f"{resource_type}:{resource_id}",
            field="snapshot",
        )
        value = dict(snapshot)
        value["rolledBackFromVersion"] = current_version
        return value

    def _next_result_version(
        self, context: AdminCommandExecutionContext, spec: AdminCommandSpec
    ) -> int:
        if spec.result_resource_type is not None:
            resource_type, resource_id = spec.output_resource(
                target_type=context.target_type,
                target_id=context.target_id,
                command_id=str(context.command_id),
            )
            _, current = self._resource_snapshot(resource_type, resource_id, context.tenant_id)
            return current + 1
        return context.expected_target_version + 1

    def _resource_snapshot(
        self, resource_type: str, resource_id: str, tenant_id: int
    ) -> tuple[dict[str, object] | None, int]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT * FROM ai_admin_control_resources
                    WHERE tenant_id = %s AND resource_type = %s AND resource_id = %s""",
                (tenant_id, resource_type, resource_id),
            ).fetchone()
        if row is None:
            return None, 0
        value = self.codec.decrypt_json(
            row["snapshot_envelope"],
            tenant_id=tenant_id,
            resource_type="admin-control-resource",
            resource_id=f"{resource_type}:{resource_id}",
            field="snapshot",
        )
        return value, int(row["resource_version"])


def _nonnegative_number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0
