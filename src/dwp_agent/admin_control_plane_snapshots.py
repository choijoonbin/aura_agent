from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .admin_control_plane_contracts import (
    CapabilityStatus,
    ConnectorsSnapshot,
    ControlPlaneCapability,
    EvaluationSafetySnapshot,
    IncidentsSnapshot,
    ModelsRoutingSnapshot,
    OutcomesSnapshot,
)
from .admin_model_routing_contracts import LatestRoutingSimulation
from .admin_control_plane_errors import AdminControlPlaneUnavailable
from .governed_domain_core import GovernedPayloadCodec


class AdminControlPlaneSnapshots:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
        except Exception as error:
            raise AdminControlPlaneUnavailable("Control-plane snapshot encryption is unavailable.") from error

    def models_routing(self, tenant_id: int) -> ModelsRoutingSnapshot:
        now = datetime.now(UTC)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                policy = connection.execute(
                    """SELECT emergency_disabled, allowed_model_routes,
                              budget_enforcement_mode, period_token_limit,
                              policy_version, updated_at
                         FROM ai_execution_policies WHERE tenant_id = %s""",
                    (tenant_id,),
                ).fetchone()
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM ai_admin_control_commands WHERE tenant_id = %s AND command_state = 'AWAITING_APPROVAL'",
                    (tenant_id,),
                ).fetchone()["count"]
                if policy is None:
                    return ModelsRoutingSnapshot(
                        generated_at=now, capability=_not_configured("No tenant AI routing policy exists."),
                        providers=[], models=[], routing_policies=[], routing_rules=[],
                        latest_simulation=None, pending_approval_count=pending,
                        active_canary_count=0, emergency_stop_active=False,
                    )
                routes = policy["allowed_model_routes"]
                if isinstance(routes, str):
                    routes = json.loads(routes)
                providers: dict[str, dict[str, object]] = {}
                models: list[dict[str, object]] = []
                model_ids: list[str] = []
                routing_rules: list[dict[str, object]] = []
                incomplete_governance = False
                for index, route in enumerate(routes):
                    provider_id = str(route["provider"])
                    model_id = f'{provider_id}:{route["model"]}'
                    model_ids.append(model_id)
                    classifications = _list_value(
                        route, "allowed_data_classifications", "allowedDataClassifications"
                    )
                    governance_policy = _text_value(
                        route, "governance_policy", "governancePolicy"
                    )
                    region = _text_value(route, "region")
                    credential_ref = _text_value(route, "credential_ref", "credentialRef")
                    credential_state = _text_value(
                        route, "credential_state", "credentialState"
                    ).upper()
                    credential_state = {
                        "ACTIVE": "BOUND", "EXPIRING": "ROTATION_DUE",
                        "REVOKED": "EXPIRED", "UNKNOWN": "MISSING",
                    }.get(credential_state, credential_state)
                    if credential_state not in {"BOUND", "ROTATION_DUE", "EXPIRED", "MISSING"}:
                        credential_state = "MISSING"
                    if not classifications or not governance_policy or not region or not credential_ref:
                        incomplete_governance = True
                    providers.setdefault(provider_id, {
                        "providerId": provider_id, "name": provider_id,
                        "kind": "MANAGED", "region": region or None,
                        "health": _health(route.get("availability_state", route.get("availabilityState"))),
                        "activeModelCount": 0, "updatedAt": policy["updated_at"],
                    })
                    providers[provider_id]["activeModelCount"] = int(providers[provider_id]["activeModelCount"]) + 1
                    models.append({
                        "modelId": model_id, "providerId": provider_id,
                        "displayName": str(route["model"]), "modalities": ["TEXT"],
                        "lifecycle": "ACTIVE",
                        "allowedDataClassifications": classifications,
                        "governancePolicy": governance_policy or "UNCONFIGURED",
                        "region": region or "UNSPECIFIED",
                        "credentialState": credential_state,
                        "credentialRef": credential_ref,
                    })
                    routing_rules.append({
                        "ruleId": _text_value(route, "rule_id", "ruleId") or f"ask-runtime-{index + 1}",
                        "name": _text_value(route, "name") or f"ASK runtime route {index + 1}",
                        "taskType": _text_value(route, "task_type", "taskType") or "ASK",
                        "conditions": _list_value(route, "conditions"),
                        "allowedDataClassifications": classifications,
                        "primaryModelId": model_id,
                        "fallbackModelIds": _model_refs(route, provider_id),
                        "failClosed": bool(route.get("fail_closed", route.get("failClosed", True))),
                        "version": int(route.get("version", policy["policy_version"])),
                    })
                budget_mode = "BLOCK" if policy["budget_enforcement_mode"] == "ENFORCED" else "WARN"
                routing = [{
                    "policyId": "ASK_RUNTIME", "name": "ASK runtime routing",
                    "scope": "ASK_RUNTIME", "primaryModelId": model_ids[0],
                    "fallbackModelIds": model_ids[1:], "budgetMode": budget_mode,
                    "dailyBudget": None, "version": policy["policy_version"],
                    "state": "ACTIVE", "updatedAt": policy["updated_at"],
                }] if model_ids else []
                reason = "Provider pricing, latency, and billing telemetry are not connected."
                recovery = "Connect verified provider telemetry to expose measured cost and latency."
                if incomplete_governance:
                    reason = "Some configured model routes lack governance, region, classification, or credential metadata."
                    recovery = "Save a governed model-routing policy with complete credential and data-classification metadata."
                capability = ControlPlaneCapability(
                    status=CapabilityStatus.PARTIAL, configured=True,
                    reason=reason, recovery_hint=recovery,
                )
                simulation_row = connection.execute(
                    """SELECT * FROM ai_admin_control_resources
                        WHERE tenant_id = %s AND resource_type = 'MODEL_ROUTE_SIMULATION'
                        ORDER BY updated_at DESC LIMIT 1""",
                    (tenant_id,),
                ).fetchone()
                latest_simulation = (
                    LatestRoutingSimulation.model_validate(self._snapshot(simulation_row))
                    if simulation_row is not None else None
                )
                return ModelsRoutingSnapshot(
                    generated_at=now, capability=capability,
                    providers=list(providers.values()), models=models,
                    routing_policies=routing, routing_rules=routing_rules,
                    latest_simulation=latest_simulation, pending_approval_count=pending,
                    active_canary_count=0,
                    emergency_stop_active=policy["emergency_disabled"],
                    monthly_spend=None,
                    monthly_budget=None,
                )
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise AdminControlPlaneUnavailable("Models and routing snapshot is unavailable.") from error

    def connectors(self, tenant_id: int) -> ConnectorsSnapshot:
        resources = self._resources(tenant_id, "CONNECTOR")
        items = [self._snapshot(row) for row in resources]
        blocked = sum(len(item.get("blockedRepositories", [])) for item in items)
        mismatch = sum(int(item.get("aclMismatchCount", 0)) for item in items)
        return ConnectorsSnapshot(
            generated_at=datetime.now(UTC), capability=_resource_capability(items, "No connector worker has reported an authoritative snapshot."),
            connectors=items, blocked_repository_count=blocked, acl_mismatch_count=mismatch,
        )

    def evaluation_safety(self, tenant_id: int) -> EvaluationSafetySnapshot:
        datasets = [self._snapshot(row) for row in self._resources(tenant_id, "EVALUATION_DATASET")]
        comparisons = [self._snapshot(row) for row in self._resources(tenant_id, "EVALUATION_COMPARISON")]
        signals = [self._snapshot(row) for row in self._resources(tenant_id, "DRIFT_SIGNAL")]
        configured = bool(datasets or comparisons or signals)
        return EvaluationSafetySnapshot(
            generated_at=datetime.now(UTC),
            capability=_resource_capability(datasets + comparisons + signals, "No evaluation worker has reported authoritative results."),
            datasets=datasets, comparisons=comparisons, drift_signals=signals,
            release_gate_state="REVIEW" if configured else "UNKNOWN",
        )

    def incidents(self, tenant_id: int) -> IncidentsSnapshot:
        incidents = [self._snapshot(row) for row in self._resources(tenant_id, "AI_INCIDENT")]
        for incident in incidents:
            event_ids = [event.get("eventId") for event in incident.get("timeline", [])]
            if len(event_ids) != len(set(event_ids)):
                raise AdminControlPlaneUnavailable("Incident timeline contains duplicate immutable event IDs.")
        return IncidentsSnapshot(
            generated_at=datetime.now(UTC),
            capability=_resource_capability(incidents, "No incident system has reported authoritative incidents."),
            incidents=incidents,
            quarantined_run_count=sum(int(item.get("quarantinedRunCount", 0)) for item in incidents),
            recovery_approval_count=sum(1 for item in incidents if item.get("state") == "RECOVERY_PENDING"),
        )

    def outcomes(
        self, tenant_id: int, period_days: int, organization: str | None,
        work_type: str | None,
    ) -> OutcomesSnapshot:
        cutoff = datetime.now(UTC) - timedelta(days=period_days)
        resources = self._resources(tenant_id, None)
        snapshots = [self._snapshot(row) for row in resources if row["updated_at"] >= cutoff]
        scoped = [item for item in snapshots if _in_scope(item, organization, work_type)]
        metrics = [item for item in scoped if item.get("resourceKind") == "OUTCOME_METRIC"]
        cohorts = [item for item in scoped if item.get("resourceKind") == "OUTCOME_COHORT"]
        backlog = [item for item in scoped if item.get("resourceKind") == "IMPROVEMENT_BACKLOG"]
        budgets = [item for item in scoped if item.get("resourceKind") == "TOKEN_BUDGET"]
        privacy_threshold = int(os.getenv("DWP_OUTCOME_PRIVACY_THRESHOLD", "10"))
        visible_cohorts = [item for item in cohorts if int(item.get("completedWorkCount", 0)) >= privacy_threshold]
        return OutcomesSnapshot(
            generated_at=datetime.now(UTC), period_days=period_days,
            capability=_resource_capability(metrics + visible_cohorts + backlog + budgets,
                                            "No measured outcome aggregates exist for this scope."),
            privacy_threshold=privacy_threshold,
            suppressed_cohort_count=len(cohorts) - len(visible_cohorts),
            metrics=metrics, cohorts=visible_cohorts, backlog=backlog,
            token_budgets=budgets, currency=os.getenv("DWP_OUTCOME_CURRENCY", "USD"),
        )

    def _resources(self, tenant_id: int, resource_type: str | None) -> list[Any]:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                return connection.execute(
                    """SELECT * FROM ai_admin_control_resources
                        WHERE tenant_id = %s AND (%s IS NULL OR resource_type = %s)
                        ORDER BY updated_at DESC""",
                    (tenant_id, resource_type, resource_type),
                ).fetchall()
        except PsycopgError as error:
            raise AdminControlPlaneUnavailable("Control-plane snapshots are unavailable.") from error

    def _snapshot(self, row: Any) -> dict[str, object]:
        value = self.codec.decrypt_json(
            row["snapshot_envelope"], tenant_id=row["tenant_id"],
            resource_type="admin-control-resource",
            resource_id=f'{row["resource_type"]}:{row["resource_id"]}', field="snapshot",
        )
        return value


def _not_configured(reason: str) -> ControlPlaneCapability:
    return ControlPlaneCapability(
        status=CapabilityStatus.NOT_CONFIGURED, configured=False, reason=reason,
        recovery_hint="Complete a governed setup command and wait for its verified worker receipt.",
    )


def _resource_capability(items: list[object], reason: str) -> ControlPlaneCapability:
    if items:
        return ControlPlaneCapability(status=CapabilityStatus.AVAILABLE, configured=True)
    return _not_configured(reason)


def _health(availability: object) -> str:
    return {"VERIFIED": "HEALTHY", "STALE": "DEGRADED", "UNAVAILABLE": "FAILED"}.get(str(availability), "UNKNOWN")


def _text_value(item: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _list_value(item: dict[str, object], *keys: str) -> list[str]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
            return list(dict.fromkeys(entry.strip() for entry in value if entry.strip()))
    return []


def _model_refs(route: dict[str, object], provider_id: str) -> list[str]:
    values = route.get("fallback_model_ids", route.get("fallbackModelIds", []))
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        value if ":" in value else f"{provider_id}:{value}"
        for value in values if isinstance(value, str) and value
    ))


def _in_scope(item: dict[str, object], organization: str | None, work_type: str | None) -> bool:
    return (
        (organization is None or item.get("organization") == organization)
        and (work_type is None or item.get("workType") == work_type)
    )


@lru_cache(maxsize=1)
def get_admin_control_plane_snapshots() -> AdminControlPlaneSnapshots:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise AdminControlPlaneUnavailable("Control-plane snapshots are unavailable.")
    return AdminControlPlaneSnapshots(database_url)
