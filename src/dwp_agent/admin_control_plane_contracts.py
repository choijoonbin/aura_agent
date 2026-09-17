from __future__ import annotations
import hashlib
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from .contract_model import ContractModel
from .ai_control_contracts import EnforcementActivationState
from .admin_control_plane_audit_contracts import GovernedCommandEvidenceInput
from .admin_evaluation_contracts import EvaluationComparisonSummary
from .admin_model_routing_contracts import LatestRoutingSimulation, ModelRouteSimulationInput, RoutingRule
from .admin_control_plane_validation import (
    normalize_command_preflight, normalize_governed_review,
    normalize_worker_completion, validate_special_admin_command,
)
class CapabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"
class OperationalHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
class ControlPlaneCapability(ContractModel):
    status: CapabilityStatus
    configured: bool
    reason: str | None = Field(default=None, max_length=500)
    recovery_hint: str | None = Field(default=None, max_length=500)
class ProviderSummary(ContractModel):
    provider_id: str
    name: str
    kind: str = Field(pattern=r"^(MANAGED|PRIVATE|ON_PREMISE)$")
    region: str | None = None
    health: OperationalHealth
    latency_p95_ms: float | None = Field(default=None, ge=0)
    success_rate: float | None = Field(default=None, ge=0, le=100)
    active_model_count: int = Field(ge=0)
    updated_at: datetime
class ModelSummary(ContractModel):
    model_id: str
    provider_id: str
    display_name: str
    modalities: list[str]
    context_window: int | None = Field(default=None, ge=1)
    lifecycle: str = Field(pattern=r"^(ACTIVE|CANARY|PAUSED|RETIRED)$")
    quality_score: float | None = Field(default=None, ge=0, le=100)
    cost_per_million_input_tokens: float | None = Field(default=None, ge=0)
    cost_per_million_output_tokens: float | None = Field(default=None, ge=0)
    allowed_data_classifications: list[str]
    governance_policy: str
    region: str
    credential_state: str = Field(pattern=r"^(BOUND|ROTATION_DUE|EXPIRED|MISSING)$")
    credential_ref: str
class RoutingPolicySummary(ContractModel):
    policy_id: str
    name: str
    scope: str
    primary_model_id: str
    fallback_model_ids: list[str]
    budget_mode: str = Field(pattern=r"^(WARN|THROTTLE|BLOCK)$")
    daily_budget: float | None = Field(default=None, gt=0)
    version: int = Field(ge=1)
    state: str = Field(pattern=r"^(ACTIVE|CANARY|PAUSED)$")
    updated_at: datetime
class ModelsRoutingSnapshot(ContractModel):
    generated_at: datetime
    capability: ControlPlaneCapability
    providers: list[ProviderSummary]
    models: list[ModelSummary]
    routing_policies: list[RoutingPolicySummary]
    routing_rules: list[RoutingRule]
    latest_simulation: LatestRoutingSimulation | None
    pending_approval_count: int = Field(ge=0)
    active_canary_count: int = Field(ge=0)
    emergency_stop_active: bool
    monthly_spend: float | None = Field(default=None, ge=0)
    monthly_budget: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_routing_closure(self) -> "ModelsRoutingSnapshot":
        model_ids = [model.model_id for model in self.models]
        rule_ids = [rule.rule_id for rule in self.routing_rules]
        if len(model_ids) != len(set(model_ids)) or len(rule_ids) != len(set(rule_ids)):
            raise ValueError("Model and routing-rule IDs must be unique.")
        known = set(model_ids)
        for rule in self.routing_rules:
            if rule.primary_model_id not in known or not set(rule.fallback_model_ids).issubset(known):
                raise ValueError("Routing rules must reference models in this snapshot.")
        if self.latest_simulation is not None:
            simulation = self.latest_simulation
            if simulation.matched_rule_id not in set(rule_ids):
                raise ValueError("Latest simulation must reference a routing rule in this snapshot.")
            referenced = set(simulation.fallback_model_ids)
            if simulation.target_model_id:
                referenced.add(simulation.target_model_id)
            if not referenced.issubset(known):
                raise ValueError("Latest simulation references an unknown model.")
        return self
class ConnectorSummary(ContractModel):
    connector_id: str
    name: str
    provider_type: str
    owner_ref: str
    tenant_scope: str
    region: str | None = None
    repositories: list[str]
    health: OperationalHealth
    sync_state: str = Field(pattern=r"^(IDLE|SYNCING|PARTIAL|FAILED|PAUSED)$")
    acl_coverage: float | None = Field(default=None, ge=0, le=100)
    last_successful_sync_at: datetime | None = None
    secret_expires_at: datetime | None = None
    version: int = Field(ge=1)
class ConnectorsSnapshot(ContractModel):
    generated_at: datetime
    capability: ControlPlaneCapability
    connectors: list[ConnectorSummary]
    blocked_repository_count: int = Field(ge=0)
    acl_mismatch_count: int = Field(ge=0)
class EvaluationDatasetSummary(ContractModel):
    dataset_id: str
    name: str
    version: int = Field(ge=1)
    owner_ref: str
    case_count: int = Field(ge=0)
    pii_state: str = Field(pattern=r"^(PENDING|PASS|REVIEW|BLOCKED)$")
    checksum_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime
class DriftSignal(ContractModel):
    signal_id: str
    label: str
    severity: str = Field(pattern=r"^(INFO|WARNING|CRITICAL)$")
    current_value: float | None = None
    threshold: float | None = None
    affected_scope: str
    detected_at: datetime
    anonymized_sample: str | None = None
    feedback_evidence_ref: str | None = None
    approved_raw_access: bool | None = None
    rollback_recommendation: str | None = None
class EvaluationSafetySnapshot(ContractModel):
    generated_at: datetime
    capability: ControlPlaneCapability
    datasets: list[EvaluationDatasetSummary]
    comparisons: list[EvaluationComparisonSummary]
    drift_signals: list[DriftSignal]
    release_gate_state: str = Field(pattern=r"^(PASS|REVIEW|BLOCKED|UNKNOWN)$")
class IncidentTimelineEvent(ContractModel):
    event_id: str
    type: str
    summary: str
    actor_ref: str | None = None
    occurred_at: datetime
    evidence_refs: list[str]
class IncidentSummary(ContractModel):
    incident_id: str
    title: str
    severity: str = Field(pattern=r"^SEV[1-4]$")
    state: str = Field(pattern=r"^(OPEN|CONTAINED|VALIDATING|RECOVERY_PENDING|RECOVERED|CLOSED)$")
    affected_run_count: int = Field(ge=0)
    affected_user_count: int | None = Field(default=None, ge=0)
    scope: str
    owner_ref: str | None = None
    correlation_id: str
    opened_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)
    timeline: list[IncidentTimelineEvent]

    @field_validator("timeline")
    @classmethod
    def unique_timeline(cls, value: list[IncidentTimelineEvent]) -> list[IncidentTimelineEvent]:
        if len({item.event_id for item in value}) != len(value):
            raise ValueError("Incident timeline event IDs must be unique.")
        return value
class IncidentsSnapshot(ContractModel):
    generated_at: datetime
    capability: ControlPlaneCapability
    incidents: list[IncidentSummary]
    quarantined_run_count: int = Field(ge=0)
    recovery_approval_count: int = Field(ge=0)
class OutcomeMetric(ContractModel):
    metric_key: str
    label: str
    value: float | None = Field(default=None, ge=0)
    unit: str = Field(pattern=r"^(COUNT|PERCENT|MILLISECONDS|CURRENCY|TOKENS)$")
    denominator: int | None = Field(default=None, ge=0)
    previous_value: float | None = Field(default=None, ge=0)
    freshness_at: datetime
class OutcomeCohort(ContractModel):
    cohort_key: str
    label: str
    completed_work_count: int = Field(ge=0)
    completion_rate: float | None = Field(default=None, ge=0, le=100)
    rework_rate: float | None = Field(default=None, ge=0, le=100)
    rollback_rate: float | None = Field(default=None, ge=0, le=100)
    cost_per_completed_work: float | None = Field(default=None, ge=0)
class ImprovementBacklogItem(ContractModel):
    item_id: str
    title: str
    owner_team: str
    priority: str = Field(pattern=r"^P[0-3]$")
    metric_evidence: str
    problem_cluster: str
    target_value: str | None = None
    linked_release: str | None = None
    state: str = Field(pattern=r"^(PROPOSED|APPROVED|IN_PROGRESS|DONE)$")
    version: int = Field(ge=1)
class TokenBudgetSummary(ContractModel):
    scope: str
    consumed_tokens: int = Field(ge=0)
    budget_tokens: int | None = Field(default=None, gt=0)
    projected_tokens: int | None = Field(default=None, ge=0)
    spike_detected: bool
    policy_mode: str = Field(pattern=r"^(WARN|THROTTLE|BLOCK)$")
    enforcement_activation_state: EnforcementActivationState
    version: int = Field(ge=1)
class OutcomesSnapshot(ContractModel):
    generated_at: datetime
    period_days: int = Field(ge=1, le=90)
    capability: ControlPlaneCapability
    privacy_threshold: int = Field(ge=1)
    suppressed_cohort_count: int = Field(ge=0)
    metrics: list[OutcomeMetric]
    cohorts: list[OutcomeCohort]
    backlog: list[ImprovementBacklogItem]
    token_budgets: list[TokenBudgetSummary]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
class GovernedCommandState(StrEnum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    ROLLED_BACK = "ROLLED_BACK"
class GovernedCommandKind(StrEnum):
    MODEL_ROUTING_DRAFT_SAVE = "MODEL_ROUTING_DRAFT_SAVE"
    MODEL_ROUTING_UPDATE = "MODEL_ROUTING_UPDATE"
    MODEL_ROUTE_SIMULATE = "MODEL_ROUTE_SIMULATE"
    MODEL_CANARY_START = "MODEL_CANARY_START"
    MODEL_ROLLBACK = "MODEL_ROLLBACK"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    EMERGENCY_RECOVERY = "EMERGENCY_RECOVERY"
    PROVIDER_CIRCUIT_BREAK = "PROVIDER_CIRCUIT_BREAK"
    MODEL_SMART_ISOLATE = "MODEL_SMART_ISOLATE"
    EMERGENCY_RECOVERY_SIMULATE = "EMERGENCY_RECOVERY_SIMULATE"
    EMERGENCY_ISOLATION_ROLLBACK = "EMERGENCY_ISOLATION_ROLLBACK"
    AGENT_DRAFT_SAVE = "AGENT_DRAFT_SAVE"
    AGENT_PROMOTE = "AGENT_PROMOTE"
    AGENT_EVALUATE = "AGENT_EVALUATE"
    AGENT_ROLLBACK = "AGENT_ROLLBACK"
    AGENT_KILL_SWITCH = "AGENT_KILL_SWITCH"
    AGENT_EVALUATION_CERT_SIGN = "AGENT_EVALUATION_CERT_SIGN"
    CONNECTOR_DRAFT_SAVE = "CONNECTOR_DRAFT_SAVE"
    CONNECTOR_CREATE = "CONNECTOR_CREATE"
    CONNECTOR_PROBE = "CONNECTOR_PROBE"
    CONNECTOR_SYNC = "CONNECTOR_SYNC"
    CONNECTOR_REINDEX = "CONNECTOR_REINDEX"
    CONNECTOR_SECRET_ROTATE = "CONNECTOR_SECRET_ROTATE"
    CONNECTOR_SCOPE_REDUCE = "CONNECTOR_SCOPE_REDUCE"
    CONNECTOR_REVOKE = "CONNECTOR_REVOKE"
    CONNECTOR_DELETE = "CONNECTOR_DELETE"
    CONNECTOR_OAUTH_REAUTHORIZE = "CONNECTOR_OAUTH_REAUTHORIZE"
    CONNECTOR_PAUSE = "CONNECTOR_PAUSE"
    CONNECTOR_QUARANTINE = "CONNECTOR_QUARANTINE"
    CONNECTOR_DRIFT_HEAL = "CONNECTOR_DRIFT_HEAL"
    CONNECTOR_KILL_SWITCH = "CONNECTOR_KILL_SWITCH"
    DATASET_IMPORT = "DATASET_IMPORT"
    DATASET_PII_DECIDE = "DATASET_PII_DECIDE"
    EVALUATION_COMPARE = "EVALUATION_COMPARE"
    SAFETY_SIMULATE = "SAFETY_SIMULATE"
    DRIFT_EVIDENCE_ATTACH = "DRIFT_EVIDENCE_ATTACH"
    DRIFT_RAW_EVIDENCE_REQUEST = "DRIFT_RAW_EVIDENCE_REQUEST"
    EVALUATION_RUN = "EVALUATION_RUN"
    EVALUATION_RERUN = "EVALUATION_RERUN"
    EVALUATION_REPORT_EXPORT = "EVALUATION_REPORT_EXPORT"
    EVALUATION_GATE_APPROVE = "EVALUATION_GATE_APPROVE"
    SAFETY_GUARDRAIL_ENFORCE = "SAFETY_GUARDRAIL_ENFORCE"
    SAFETY_CANARY_APPROVE = "SAFETY_CANARY_APPROVE"
    INCIDENT_EMERGENCY_STOP = "INCIDENT_EMERGENCY_STOP"
    INCIDENT_WAR_ROOM_OPEN = "INCIDENT_WAR_ROOM_OPEN"
    INCIDENT_REPORT_EXPORT = "INCIDENT_REPORT_EXPORT"
    INCIDENT_VALIDATION_RUN = "INCIDENT_VALIDATION_RUN"
    INCIDENT_CONNECTOR_REAUTH = "INCIDENT_CONNECTOR_REAUTH"
    INCIDENT_SAFE_ROLLBACK = "INCIDENT_SAFE_ROLLBACK"
    INCIDENT_RECOVERY_RESYNC = "INCIDENT_RECOVERY_RESYNC"
    INCIDENT_SKIP_QUARANTINED = "INCIDENT_SKIP_QUARANTINED"
    INCIDENT_ROUTINE_PAUSE = "INCIDENT_ROUTINE_PAUSE"
    INCIDENT_CONTAIN = "INCIDENT_CONTAIN"
    RUN_QUARANTINE = "RUN_QUARANTINE"
    RUN_REPLAY = "RUN_REPLAY"
    RUN_COMPENSATE = "RUN_COMPENSATE"
    INCIDENT_RECOVERY = "INCIDENT_RECOVERY"
    INCIDENT_CLOSE = "INCIDENT_CLOSE"
    BACKLOG_CREATE = "BACKLOG_CREATE"
    BACKLOG_UPDATE = "BACKLOG_UPDATE"
    BACKLOG_TICKET_OPEN = "BACKLOG_TICKET_OPEN"
    BACKLOG_RELEASE_LINK = "BACKLOG_RELEASE_LINK"
    OUTCOME_EXPORT = "OUTCOME_EXPORT"
    COST_SIMULATE = "COST_SIMULATE"
    TOKEN_BUDGET_UPDATE = "TOKEN_BUDGET_UPDATE"
class CommandTarget(ContractModel):
    type: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z0-9_.:-]+$")
    id: str = Field(min_length=1, max_length=160)
class PreflightChange(ContractModel):
    field: str = Field(min_length=1, max_length=160)
    before: str = Field(max_length=4_000)
    after: str = Field(max_length=4_000)
class CommandPreflight(ContractModel):
    changes: list[PreflightChange] = Field(min_length=1, max_length=100)
    impact_scopes: list[str] = Field(min_length=1, max_length=100)
    recovery_plan: str = Field(min_length=5, max_length=8_000)
    recovery_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verify_recovery_hash(self) -> "CommandPreflight":
        self.impact_scopes, self.recovery_plan = normalize_command_preflight(
            self.changes, self.impact_scopes, self.recovery_plan)
        observed = hashlib.sha256(self.recovery_plan.encode("utf-8")).hexdigest()
        if observed != self.recovery_plan_hash:
            raise ValueError("recoveryPlanHash does not match recoveryPlan.")
        return self
class CreateGovernedCommandRequest(ContractModel):
    command_id: UUID
    kind: GovernedCommandKind
    target: CommandTarget
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=5, max_length=2_000)
    ticket_ref: str = Field(min_length=1, max_length=240)
    evidence_refs: list[str] = Field(max_length=100)
    impact_acknowledged: bool
    preflight: CommandPreflight
    payload: dict[str, JsonValue] = Field(max_length=200)

    @model_validator(mode="after")
    def acknowledged(self) -> "CreateGovernedCommandRequest":
        self.reason, self.evidence_refs, self.ticket_ref = normalize_governed_review(
            self.reason, self.evidence_refs, self.ticket_ref)
        if not self.impact_acknowledged:
            raise ValueError("impactAcknowledged must be true.")
        if (
            self.kind == GovernedCommandKind.EMERGENCY_RECOVERY
            and self.payload.get("requireIndependentSecondFactor") is not True
        ):
            raise ValueError("Emergency recovery requires an independent second factor.")
        if self.kind == GovernedCommandKind.MODEL_ROUTE_SIMULATE:
            ModelRouteSimulationInput.model_validate(self.payload)
        validate_special_admin_command(
            kind=self.kind.value, target_type=self.target.type, target_id=self.target.id,
            expected_version=self.expected_version, payload=self.payload,
        )
        return self
class CommandReview(ContractModel):
    reason: str
    ticket_ref: str
    evidence_refs: list[str]
    preflight: CommandPreflight
class CommandDecision(ContractModel):
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    actor_user_id: str
    reason: str
    evidence_refs: list[str]
    decided_at: datetime
class CommandReceipt(ContractModel):
    receipt_id: UUID
    audit_event_id: UUID
    completed_at: datetime
    result_summary: str
    domain_receipt_ref: str | None = None
    rollback_ref: str | None = None
class CommandProblem(ContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$")
    detail: str
    recovery_hint: str | None = None
class GovernedCommand(ContractModel):
    command_id: UUID
    kind: GovernedCommandKind
    state: GovernedCommandState
    target: CommandTarget
    expected_version: int = Field(ge=0)
    maker_user_id: str
    checker_user_id: str | None = None
    approval_required: bool
    review: CommandReview
    allowed_transitions: list[str]
    transition_block_reason: str | None = None
    can_approve: bool
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    receipt: CommandReceipt | None = None
    problem: CommandProblem | None = None
    version: int = Field(ge=1)
    decision: CommandDecision | None = None
class GovernedCommandDecisionRequest(GovernedCommandEvidenceInput):
    command_id: UUID
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    expected_version: int = Field(ge=1)
class GovernedCommandTransitionRequest(GovernedCommandEvidenceInput):
    command_id: UUID
    expected_version: int = Field(ge=1)
class GovernedCommandRestartRequest(GovernedCommandTransitionRequest):
    pass
class GovernedCommandObservation(ContractModel):
    command_id: UUID
    attempt_id: UUID | None = None
    expected_version: int = Field(ge=1)
    tenant_id: int | None = Field(default=None, ge=1)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=160)
    state: GovernedCommandState
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    result_summary: str | None = Field(default=None, max_length=2_000)
    domain_receipt_ref: str | None = Field(default=None, max_length=500)
    rollback_ref: str | None = Field(default=None, max_length=500)
    result_snapshot: dict[str, JsonValue] | None = None
    result_version: int | None = Field(default=None, ge=1)
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    problem: CommandProblem | None = None

    @model_validator(mode="after")
    def verified_completion(self) -> "GovernedCommandObservation":
        terminal_success = self.state in {
            GovernedCommandState.SUCCEEDED,
            GovernedCommandState.ROLLED_BACK,
        }
        if terminal_success:
            (self.correlation_id, self.result_summary, self.domain_receipt_ref) = (
                normalize_worker_completion(
                    tenant_id=self.tenant_id, attempt_id=self.attempt_id,
                    correlation_id=self.correlation_id,
                    summary=self.result_summary, receipt_ref=self.domain_receipt_ref,
                    snapshot=self.result_snapshot, result_version=self.result_version,
                    result_sha256=self.result_sha256,
                )
            )
        if self.state == GovernedCommandState.ROLLED_BACK and not (
            self.rollback_ref and self.rollback_ref.strip()
        ):
            raise ValueError("A rollback worker observation must link the receipt it reverses.")
        if self.rollback_ref:
            self.rollback_ref = self.rollback_ref.strip()
        if self.state in {GovernedCommandState.PARTIAL, GovernedCommandState.FAILED} and self.problem is None:
            raise ValueError("Incomplete worker observations require a safe problem.")
        if self.state not in {
            GovernedCommandState.RUNNING, GovernedCommandState.PARTIAL,
            GovernedCommandState.SUCCEEDED, GovernedCommandState.FAILED,
            GovernedCommandState.ROLLED_BACK,
        }:
            raise ValueError("The worker cannot report this command state.")
        return self
class AdminCommandCapability(ContractModel):
    kind: GovernedCommandKind
    family: str = Field(pattern=r"^A0[1-6]$")
    execution_mode: str = Field(pattern=r"^(INTERNAL|EXTERNAL_ADAPTER)$")
    status: CapabilityStatus
    configured: bool
    reason: str | None = Field(default=None, max_length=500)
    recovery_hint: str | None = Field(default=None, max_length=500)
class AdminCommandCapabilitiesSnapshot(ContractModel):
    generated_at: datetime
    worker_available: bool
    commands: list[AdminCommandCapability]

    @model_validator(mode="after")
    def exhaustive(self) -> "AdminCommandCapabilitiesSnapshot":
        observed = [entry.kind for entry in self.commands]
        if len(observed) != len(set(observed)) or set(observed) != set(GovernedCommandKind):
            raise ValueError("Admin command capabilities must cover every governed command kind once.")
        return self


class AdminCommandCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON admin command capabilities loaded."
    success: bool = True
    data: AdminCommandCapabilitiesSnapshot
class GovernedCommandEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON governed command loaded."
    success: bool = True
    data: GovernedCommand
class GovernedCommandsSnapshot(ContractModel):
    generated_at: datetime
    commands: list[GovernedCommand]
class GovernedCommandsEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON governed commands loaded."
    success: bool = True
    data: GovernedCommandsSnapshot
class SnapshotEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON control-plane snapshot loaded."
    success: bool = True
    data: ModelsRoutingSnapshot | ConnectorsSnapshot | EvaluationSafetySnapshot | IncidentsSnapshot | OutcomesSnapshot
