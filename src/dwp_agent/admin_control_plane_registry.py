from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .admin_control_plane_contracts import GovernedCommandKind


class AdminCommandExecutionMode(StrEnum):
    INTERNAL = "INTERNAL"
    EXTERNAL_ADAPTER = "EXTERNAL_ADAPTER"


class AdminCommandResourceStrategy(StrEnum):
    """Defines which authoritative version fences a governed command."""

    TARGET = "TARGET"
    CREATE_TARGET = "CREATE_TARGET"
    RESULT = "RESULT"
    AI_POLICY = "AI_POLICY"
    DELEGATED_TARGET = "DELEGATED_TARGET"


@dataclass(frozen=True)
class AdminCommandSpec:
    kind: GovernedCommandKind
    family: str
    mode: AdminCommandExecutionMode
    operation: str
    service: str | None = None
    resource_strategy: AdminCommandResourceStrategy = AdminCommandResourceStrategy.TARGET
    result_resource_type: str | None = None
    allow_unversioned_target: bool = False

    def output_resource(self, *, target_type: str, target_id: str, command_id: str) -> tuple[str, str]:
        if self.resource_strategy == AdminCommandResourceStrategy.RESULT:
            return self.result_resource_type or f"{self.kind.value}_RESULT", command_id
        return self.result_resource_type or target_type, target_id


def _internal(
    kind: GovernedCommandKind,
    family: str,
    operation: str,
    *,
    strategy: AdminCommandResourceStrategy = AdminCommandResourceStrategy.TARGET,
    result_resource_type: str | None = None,
    allow_unversioned_target: bool = False,
) -> AdminCommandSpec:
    return AdminCommandSpec(
        kind=kind,
        family=family,
        mode=AdminCommandExecutionMode.INTERNAL,
        operation=operation,
        resource_strategy=strategy,
        result_resource_type=result_resource_type,
        allow_unversioned_target=allow_unversioned_target,
    )


def _external(
    kind: GovernedCommandKind,
    family: str,
    service: str,
    *,
    strategy: AdminCommandResourceStrategy = AdminCommandResourceStrategy.TARGET,
    result_resource_type: str | None = None,
) -> AdminCommandSpec:
    return AdminCommandSpec(
        kind=kind,
        family=family,
        mode=AdminCommandExecutionMode.EXTERNAL_ADAPTER,
        operation="EXTERNAL",
        service=service,
        resource_strategy=strategy,
        result_resource_type=result_resource_type,
    )


S = AdminCommandResourceStrategy
K = GovernedCommandKind

# This registry is intentionally exhaustive. A missing kind fails import and therefore cannot
# silently fall through to a fabricated success response.
ADMIN_COMMAND_REGISTRY: dict[GovernedCommandKind, AdminCommandSpec] = {
    # A-01 models, routing, budget and emergency control.
    K.MODEL_ROUTING_DRAFT_SAVE: _internal(
        K.MODEL_ROUTING_DRAFT_SAVE, "A01", "MERGE", strategy=S.AI_POLICY,
        result_resource_type="ROUTING_POLICY_DRAFT",
    ),
    K.MODEL_ROUTING_UPDATE: _internal(K.MODEL_ROUTING_UPDATE, "A01", "MODEL_ROUTING_UPDATE", strategy=S.AI_POLICY),
    K.MODEL_ROUTE_SIMULATE: _internal(
        K.MODEL_ROUTE_SIMULATE, "A01", "MODEL_ROUTE_SIMULATE", strategy=S.AI_POLICY,
        result_resource_type="MODEL_ROUTE_SIMULATION", allow_unversioned_target=True,
    ),
    K.MODEL_CANARY_START: _external(
        K.MODEL_CANARY_START, "A01", "model-routing", strategy=S.AI_POLICY,
        result_resource_type="MODEL_CANARY",
    ),
    K.MODEL_ROLLBACK: _external(
        K.MODEL_ROLLBACK, "A01", "model-routing", strategy=S.AI_POLICY,
        result_resource_type="MODEL_ROLLBACK_RECEIPT",
    ),
    K.EMERGENCY_STOP: _internal(K.EMERGENCY_STOP, "A01", "EMERGENCY_STOP", strategy=S.AI_POLICY),
    K.EMERGENCY_RECOVERY: _internal(K.EMERGENCY_RECOVERY, "A01", "EMERGENCY_RECOVERY", strategy=S.AI_POLICY),
    K.PROVIDER_CIRCUIT_BREAK: _external(
        K.PROVIDER_CIRCUIT_BREAK, "A01", "model-routing", strategy=S.AI_POLICY,
        result_resource_type="PROVIDER_CIRCUIT",
    ),
    K.MODEL_SMART_ISOLATE: _external(
        K.MODEL_SMART_ISOLATE, "A01", "model-routing", strategy=S.AI_POLICY,
        result_resource_type="MODEL_ISOLATION",
    ),
    K.EMERGENCY_RECOVERY_SIMULATE: _internal(
        K.EMERGENCY_RECOVERY_SIMULATE, "A01", "SIMULATION", strategy=S.AI_POLICY,
        result_resource_type="EMERGENCY_RECOVERY_SIMULATION",
    ),
    K.EMERGENCY_ISOLATION_ROLLBACK: _external(
        K.EMERGENCY_ISOLATION_ROLLBACK, "A01", "model-routing", strategy=S.AI_POLICY,
        result_resource_type="EMERGENCY_ISOLATION_ROLLBACK",
    ),
    # A-02 governed agent lifecycle.
    K.AGENT_DRAFT_SAVE: _external(
        K.AGENT_DRAFT_SAVE, "A02", "agent-registry", strategy=S.DELEGATED_TARGET,
        result_resource_type="AGENT_DRAFT",
    ),
    K.AGENT_PROMOTE: _external(
        K.AGENT_PROMOTE, "A02", "agent-registry", strategy=S.DELEGATED_TARGET
    ),
    K.AGENT_EVALUATE: _external(
        K.AGENT_EVALUATE, "A02", "agent-evaluation", strategy=S.DELEGATED_TARGET,
        result_resource_type="AGENT_EVALUATION",
    ),
    K.AGENT_ROLLBACK: _external(
        K.AGENT_ROLLBACK, "A02", "agent-registry", strategy=S.DELEGATED_TARGET
    ),
    K.AGENT_KILL_SWITCH: _external(
        K.AGENT_KILL_SWITCH, "A02", "agent-registry", strategy=S.DELEGATED_TARGET
    ),
    K.AGENT_EVALUATION_CERT_SIGN: _external(
        K.AGENT_EVALUATION_CERT_SIGN, "A02", "evidence-signing",
        strategy=S.DELEGATED_TARGET,
        result_resource_type="AGENT_EVALUATION_CERTIFICATE",
    ),
    # A-03 connectors.
    K.CONNECTOR_DRAFT_SAVE: _internal(
        K.CONNECTOR_DRAFT_SAVE, "A03", "MERGE",
        result_resource_type="CONNECTOR_DRAFT",
    ),
    K.CONNECTOR_CREATE: _external(K.CONNECTOR_CREATE, "A03", "connector", strategy=S.CREATE_TARGET),
    K.CONNECTOR_PROBE: _external(K.CONNECTOR_PROBE, "A03", "connector"),
    K.CONNECTOR_SYNC: _external(K.CONNECTOR_SYNC, "A03", "connector"),
    K.CONNECTOR_REINDEX: _external(K.CONNECTOR_REINDEX, "A03", "connector"),
    K.CONNECTOR_SECRET_ROTATE: _external(K.CONNECTOR_SECRET_ROTATE, "A03", "connector"),
    K.CONNECTOR_SCOPE_REDUCE: _external(K.CONNECTOR_SCOPE_REDUCE, "A03", "connector"),
    K.CONNECTOR_REVOKE: _external(K.CONNECTOR_REVOKE, "A03", "connector"),
    K.CONNECTOR_DELETE: _external(K.CONNECTOR_DELETE, "A03", "connector"),
    K.CONNECTOR_OAUTH_REAUTHORIZE: _external(K.CONNECTOR_OAUTH_REAUTHORIZE, "A03", "connector"),
    K.CONNECTOR_PAUSE: _external(K.CONNECTOR_PAUSE, "A03", "connector"),
    K.CONNECTOR_QUARANTINE: _external(K.CONNECTOR_QUARANTINE, "A03", "connector"),
    K.CONNECTOR_DRIFT_HEAL: _external(K.CONNECTOR_DRIFT_HEAL, "A03", "connector"),
    K.CONNECTOR_KILL_SWITCH: _external(K.CONNECTOR_KILL_SWITCH, "A03", "connector"),
    # A-04 evaluation and safety. Evidence attachment is a local immutable binding;
    # evaluation execution and safety enforcement require their named domain adapters.
    K.DATASET_IMPORT: _external(K.DATASET_IMPORT, "A04", "evaluation", strategy=S.CREATE_TARGET),
    K.DATASET_PII_DECIDE: _internal(K.DATASET_PII_DECIDE, "A04", "DATASET_PII_DECIDE"),
    K.EVALUATION_COMPARE: _external(
        K.EVALUATION_COMPARE, "A04", "evaluation", strategy=S.RESULT,
        result_resource_type="EVALUATION_COMPARISON",
    ),
    K.SAFETY_SIMULATE: _external(
        K.SAFETY_SIMULATE, "A04", "safety", strategy=S.RESULT,
        result_resource_type="SAFETY_SIMULATION",
    ),
    K.DRIFT_EVIDENCE_ATTACH: _internal(
        K.DRIFT_EVIDENCE_ATTACH, "A04", "EVIDENCE_ATTACH", strategy=S.TARGET,
        result_resource_type="DRIFT_EVIDENCE", allow_unversioned_target=True,
    ),
    K.DRIFT_RAW_EVIDENCE_REQUEST: _external(
        K.DRIFT_RAW_EVIDENCE_REQUEST, "A04", "approval", strategy=S.RESULT,
        result_resource_type="DRIFT_EVIDENCE_ACCESS",
    ),
    K.EVALUATION_RUN: _external(
        K.EVALUATION_RUN, "A04", "evaluation", strategy=S.RESULT,
        result_resource_type="EVALUATION_RUN",
    ),
    K.EVALUATION_RERUN: _external(
        K.EVALUATION_RERUN, "A04", "evaluation", strategy=S.RESULT,
        result_resource_type="EVALUATION_RUN",
    ),
    K.EVALUATION_REPORT_EXPORT: _external(
        K.EVALUATION_REPORT_EXPORT, "A04", "export", strategy=S.RESULT,
        result_resource_type="EVALUATION_REPORT",
    ),
    K.EVALUATION_GATE_APPROVE: _external(K.EVALUATION_GATE_APPROVE, "A04", "approval"),
    K.SAFETY_GUARDRAIL_ENFORCE: _external(K.SAFETY_GUARDRAIL_ENFORCE, "A04", "safety"),
    K.SAFETY_CANARY_APPROVE: _external(K.SAFETY_CANARY_APPROVE, "A04", "safety"),
    # A-05 incidents. Closing a validated incident is an authoritative local ledger update;
    # containment, replay and recovery have external operational side effects.
    K.INCIDENT_EMERGENCY_STOP: _external(K.INCIDENT_EMERGENCY_STOP, "A05", "incident"),
    K.INCIDENT_WAR_ROOM_OPEN: _external(
        K.INCIDENT_WAR_ROOM_OPEN, "A05", "collaboration", strategy=S.RESULT,
        result_resource_type="INCIDENT_WAR_ROOM",
    ),
    K.INCIDENT_REPORT_EXPORT: _external(
        K.INCIDENT_REPORT_EXPORT, "A05", "export", strategy=S.RESULT,
        result_resource_type="INCIDENT_REPORT",
    ),
    K.INCIDENT_VALIDATION_RUN: _external(
        K.INCIDENT_VALIDATION_RUN, "A05", "incident", strategy=S.RESULT,
        result_resource_type="INCIDENT_VALIDATION",
    ),
    K.INCIDENT_CONNECTOR_REAUTH: _external(K.INCIDENT_CONNECTOR_REAUTH, "A05", "connector"),
    K.INCIDENT_SAFE_ROLLBACK: _external(K.INCIDENT_SAFE_ROLLBACK, "A05", "incident"),
    K.INCIDENT_RECOVERY_RESYNC: _external(K.INCIDENT_RECOVERY_RESYNC, "A05", "incident"),
    K.INCIDENT_SKIP_QUARANTINED: _external(K.INCIDENT_SKIP_QUARANTINED, "A05", "incident"),
    K.INCIDENT_ROUTINE_PAUSE: _external(K.INCIDENT_ROUTINE_PAUSE, "A05", "routine"),
    K.INCIDENT_CONTAIN: _external(K.INCIDENT_CONTAIN, "A05", "incident"),
    K.RUN_QUARANTINE: _external(K.RUN_QUARANTINE, "A05", "incident"),
    K.RUN_REPLAY: _external(K.RUN_REPLAY, "A05", "incident"),
    K.RUN_COMPENSATE: _external(K.RUN_COMPENSATE, "A05", "incident"),
    K.INCIDENT_RECOVERY: _external(K.INCIDENT_RECOVERY, "A05", "incident"),
    K.INCIDENT_CLOSE: _internal(K.INCIDENT_CLOSE, "A05", "INCIDENT_CLOSE"),
    # A-06 outcomes and cost.
    K.BACKLOG_CREATE: _internal(K.BACKLOG_CREATE, "A06", "BACKLOG", strategy=S.CREATE_TARGET),
    K.BACKLOG_UPDATE: _internal(K.BACKLOG_UPDATE, "A06", "BACKLOG"),
    K.BACKLOG_TICKET_OPEN: _external(
        K.BACKLOG_TICKET_OPEN, "A06", "ticketing", strategy=S.RESULT,
        result_resource_type="BACKLOG_TICKET",
    ),
    K.BACKLOG_RELEASE_LINK: _internal(K.BACKLOG_RELEASE_LINK, "A06", "BACKLOG_RELEASE_LINK"),
    K.OUTCOME_EXPORT: _external(
        K.OUTCOME_EXPORT, "A06", "export", strategy=S.RESULT,
        result_resource_type="OUTCOME_EXPORT",
    ),
    K.COST_SIMULATE: _internal(
        K.COST_SIMULATE, "A06", "COST_SIMULATE", strategy=S.RESULT,
        result_resource_type="COST_SIMULATION",
    ),
    K.TOKEN_BUDGET_UPDATE: _internal(
        K.TOKEN_BUDGET_UPDATE,
        "A06",
        "TOKEN_BUDGET",
        strategy=S.AI_POLICY,
    ),
}


if set(ADMIN_COMMAND_REGISTRY) != set(GovernedCommandKind):
    missing = sorted(kind.value for kind in set(GovernedCommandKind) - set(ADMIN_COMMAND_REGISTRY))
    extra = sorted(kind.value for kind in set(ADMIN_COMMAND_REGISTRY) - set(GovernedCommandKind))
    raise RuntimeError(f"Admin command registry is not exhaustive; missing={missing}, extra={extra}")


def command_spec(kind: GovernedCommandKind | str) -> AdminCommandSpec:
    try:
        normalized = kind if isinstance(kind, GovernedCommandKind) else GovernedCommandKind(kind)
        return ADMIN_COMMAND_REGISTRY[normalized]
    except (KeyError, ValueError) as error:
        raise ValueError("The admin command kind is not registered.") from error
