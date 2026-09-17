from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from .admin_control_plane_contracts import CommandTarget, GovernedCommandKind
from .canonical_json import canonical_json_bytes
from .contract_model import ContractModel


class GovernedExternalCommandResult(ContractModel):
    schema_version: Literal[1]
    command_id: UUID
    attempt_id: UUID
    tenant_id: int = Field(ge=1)
    correlation_id: str = Field(min_length=1, max_length=160)
    kind: GovernedCommandKind
    target: CommandTarget
    expected_version: int = Field(ge=0)
    request_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_type: str = Field(min_length=1, max_length=80)
    resource_id: str = Field(min_length=1, max_length=160)
    result_version: int = Field(ge=1)
    state: Literal["COMPLETED", "ROLLED_BACK"]
    outcome: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    provider_receipt_id: str = Field(min_length=1, max_length=240)
    completed_at: datetime
    evidence_refs: list[str] = Field(min_length=1, max_length=50)
    result: dict[str, JsonValue] = Field(min_length=1, max_length=200)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("correlation_id", "provider_receipt_id")
    @classmethod
    def normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("External result identifiers cannot contain outer whitespace.")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def normalized_evidence(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() or len(value) > 240 for value in values):
            raise ValueError("External result evidence references are invalid.")
        if len(set(values)) != len(values):
            raise ValueError("External result evidence references must be unique.")
        return values

    @model_validator(mode="after")
    def trustworthy_result(self) -> "GovernedExternalCommandResult":
        if self.completed_at.tzinfo is None:
            raise ValueError("External result completion time must include a timezone.")
        if self.completed_at.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("External result completion time cannot be in the future.")
        if hashlib.sha256(canonical_json_bytes(self.result)).hexdigest() != self.result_sha256:
            raise ValueError("External typed result digest does not match its payload.")
        return self


class GovernedEffectResult(ContractModel):
    @model_validator(mode="after")
    def normalized_effect_values(self) -> "GovernedEffectResult":
        for value in self.__dict__.values():
            if isinstance(value, str) and (not value.strip() or value != value.strip()):
                raise ValueError("Typed effect strings must be normalized and non-blank.")
            if isinstance(value, datetime) and value.tzinfo is None:
                raise ValueError("Typed effect timestamps must include a timezone.")
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                if any(not item.strip() or item != item.strip() for item in value):
                    raise ValueError("Typed effect lists cannot contain blank values.")
                if len(value) != len(set(value)):
                    raise ValueError("Typed effect list values must be unique.")
        return self


class GovernedExternalRollbackResult(GovernedEffectResult):
    rollback_of_receipt_id: str = Field(min_length=1, max_length=240)
    restored_resource_version: int = Field(ge=1)
    rollback_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["ROLLED_BACK"]


class ModelCanaryResult(GovernedEffectResult):
    policy_id: str
    base_policy_version: int = Field(ge=1)
    applied_policy_version: int = Field(ge=1)
    traffic_percent: float = Field(gt=0, le=100)
    primary_model_id: str
    fallback_model_ids: list[str]
    budget_mode: str
    daily_budget: float | None = Field(default=None, ge=0)
    modalities: list[str]
    agent_scopes: list[str]
    in_flight_policy: str
    canary_state: Literal["STARTED", "ACTIVE"]
    started_at: datetime
    expires_at: datetime
    evidence_ref: str

    @model_validator(mode="after")
    def valid_window(self) -> "ModelCanaryResult":
        if self.expires_at <= self.started_at:
            raise ValueError("Model canary expiry must follow its start time.")
        return self


class ModelRollbackResult(GovernedEffectResult):
    policy_id: str
    from_version: int = Field(ge=1)
    restored_version: int = Field(ge=1)
    active_primary_model_id: str
    active_fallback_model_ids: list[str]
    rollback_of_receipt_id: str
    in_flight_policy: str
    state: Literal["ROLLED_BACK"]


class ProviderCircuitResult(GovernedEffectResult):
    provider_id: str
    circuit_state: Literal["OPEN"]
    in_flight_policy: str
    fallback_route_ids: list[str]
    effective_at: datetime
    evidence_ref: str


class ModelIsolationResult(GovernedEffectResult):
    model_id: str
    serving_state: Literal["ISOLATED"]
    fallback_model_ids: list[str]
    in_flight_outcome: str
    effective_at: datetime


class IsolationRollbackResult(GovernedEffectResult):
    routing_scope: str
    policy_id: str
    isolation_state: Literal["ROLLED_BACK", "RESTORED"]
    validation_status: str
    validation_receipt_id: str
    validation_required: bool


class AgentDraftResult(GovernedEffectResult):
    agent_revision_id: str
    base_version: int = Field(ge=0)
    saved_version: int = Field(ge=1)
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle: Literal["DRAFT_SAVED"]


class AgentPromotionResult(GovernedEffectResult):
    agent_revision_id: str
    prior_lifecycle: str
    lifecycle: Literal["CANARY", "ACTIVE"]
    rollout_percent: float = Field(gt=0, le=100)
    evaluation_evidence_ref: str
    applied_version: int = Field(ge=1)


class AgentEvaluationResult(GovernedEffectResult):
    agent_revision_id: str
    evaluation_run_id: str
    pinned_draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suite_id: str
    suite_version: str
    outcome: Literal["PASS", "FAIL", "REVIEW"]
    metrics: dict[str, float]
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentCertificateResult(GovernedEffectResult):
    agent_revision_id: str
    evaluation_run_id: str
    certificate_id: str
    attestation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_id: str
    algorithm: str
    signature: str


class AgentRollbackResult(GovernedEffectResult):
    agent_revision_id: str
    from_version: int = Field(ge=1)
    restored_revision: str
    restored_version: int = Field(ge=1)
    rollback_of_receipt_id: str
    lifecycle: str


class AgentKillSwitchResult(GovernedEffectResult):
    agent_revision_id: str
    lifecycle: Literal["DISABLED", "KILLED"]
    in_flight_handling: str
    effective_at: datetime
    evidence_ref: str


class SafetySimulationResult(GovernedEffectResult):
    simulation_id: str
    safety_policy_id: str
    suites: list[str] = Field(min_length=1)
    per_suite_verdicts: dict[str, str]
    overall_verdict: Literal["PASS", "FAIL", "REVIEW"]
    sample_classification: str
    result_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DriftEvidenceAccessResult(GovernedEffectResult):
    signal_id: str
    access_grant_id: str
    permission: Literal["TIME_BOUND_READ"]
    state: Literal["GRANTED"]
    expires_at: datetime
    evidence_ref: str


class EvaluationReportResult(GovernedEffectResult):
    export_id: str
    artifact_id: str
    dataset_id: str
    dataset_version: int = Field(ge=1)
    comparison_id: str
    format: str
    include_evidence: bool
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=1)
    expires_at: datetime


class SafetyGuardrailResult(GovernedEffectResult):
    scope: str
    guardrail_policy_version: int = Field(ge=1)
    enforcement_state: Literal["ENFORCED"]
    in_flight_policy: str
    fail_closed: bool
    effective_at: datetime
    evidence_ref: str


class SafetyCanaryResult(GovernedEffectResult):
    scope: str
    canary_id: str
    traffic_percent: float = Field(gt=0, le=100)
    duration_minutes: int = Field(ge=1)
    starts_at: datetime
    expires_at: datetime
    auto_stop: bool
    state: Literal["APPROVED", "ACTIVE"]
    evaluation_evidence_ref: str

    @model_validator(mode="after")
    def valid_window(self) -> "SafetyCanaryResult":
        if self.expires_at <= self.starts_at:
            raise ValueError("Safety canary expiry must follow its start time.")
        return self


class IncidentWarRoomResult(GovernedEffectResult):
    incident_id: str
    correlation_id: str
    war_room_id: str
    war_room_ref: str
    participant_scope: list[str] = Field(min_length=1)
    timeline_bound: bool
    created_at: datetime


class IncidentArtifact(GovernedEffectResult):
    format: str
    id: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=1)
    expires_at: datetime


class IncidentReportResult(GovernedEffectResult):
    export_id: str
    incident_id: str
    correlation_id: str
    incident_version: int = Field(ge=1)
    formats: list[str] = Field(min_length=1)
    include_timeline: bool
    artifacts: list[IncidentArtifact] = Field(min_length=1)


class IncidentValidationCheck(GovernedEffectResult):
    name: str
    outcome: Literal["PASS", "FAIL", "REVIEW"]
    evidence_ref: str


class IncidentValidationResult(GovernedEffectResult):
    validation_run_id: str
    incident_id: str
    correlation_id: str
    incident_version: int = Field(ge=1)
    checks: list[IncidentValidationCheck] = Field(min_length=1)
    canary_percent: float = Field(ge=0, le=100)
    re_quarantine_on_failure: bool
    overall_outcome: Literal["PASS", "FAIL", "REVIEW"]
    completed_at: datetime


class BacklogTicketResult(GovernedEffectResult):
    item_id: str
    source_version: int = Field(ge=1)
    ticket_id: str
    ticket_system: str
    ticket_ref: str
    ticket_state: str
    title: str
    owner_team: str
    priority: str = Field(pattern=r"^P[0-3]$")
    metric_evidence: str
    target_value: str | None = None
    created_at: datetime


class OutcomeExportResult(GovernedEffectResult):
    export_id: str
    period_days: int = Field(ge=1, le=90)
    organization: str | None = None
    work_type: str | None = None
    privacy_threshold: int = Field(ge=1)
    format: str
    suppressed_cohort_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=1)
    expires_at: datetime
    download_ref: str


class ConnectorOperationResult(GovernedEffectResult):
    connector_id: str
    connector_version: int = Field(ge=1)
    effect: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    applied_payload: dict[str, JsonValue] = Field(min_length=1, max_length=100)
    provider_operation_id: str
    effect_receipt_id: str
    completed_at: datetime


class DatasetImportResult(GovernedEffectResult):
    dataset_id: str
    dataset_version: int = Field(ge=1)
    import_id: str
    name: str
    owner_ref: str
    format: Literal["CSV", "JSON"]
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_mapping: str
    pii_handling: str
    pii_state: Literal["PENDING", "REVIEW"]
    imported_case_count: int = Field(ge=0)
    completed_at: datetime


class EvaluationComparisonResult(GovernedEffectResult):
    comparison_id: str
    dataset_id: str
    dataset_version: int = Field(ge=1)
    baseline: str
    candidate: str
    prompt_version: str
    policy_version: str
    tool_version: str
    evaluator_version: str
    state: Literal["COMPLETED"]
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime


class EvaluationExecutionResult(GovernedEffectResult):
    run_id: str
    dataset_id: str
    dataset_version: int = Field(ge=1)
    pinned: bool | None = None
    comparison_id: str
    preserve_pinned_versions: bool | None = None
    state: Literal["COMPLETED"]
    result_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime


class EvaluationGateResult(GovernedEffectResult):
    dataset_id: str
    dataset_version: int = Field(ge=1)
    comparison_id: str
    dataset_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["REQUEST_APPROVAL"]
    gate_state: Literal["APPROVAL_REQUESTED"]
    approval_receipt_id: str
    completed_at: datetime


class IncidentOperationResult(GovernedEffectResult):
    incident_id: str
    incident_version: int = Field(ge=1)
    correlation_id: str
    effect: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    applied_payload: dict[str, JsonValue] = Field(min_length=1, max_length=100)
    provider_operation_id: str
    effect_receipt_id: str
    completed_at: datetime


GOVERNED_EXTERNAL_RESULTS = {
    GovernedCommandKind.MODEL_CANARY_START: ("MODEL_CANARY_STARTED", ModelCanaryResult),
    GovernedCommandKind.MODEL_ROLLBACK: ("MODEL_ROLLED_BACK", ModelRollbackResult),
    GovernedCommandKind.PROVIDER_CIRCUIT_BREAK: ("PROVIDER_CIRCUIT_BROKEN", ProviderCircuitResult),
    GovernedCommandKind.MODEL_SMART_ISOLATE: ("MODEL_ISOLATED", ModelIsolationResult),
    GovernedCommandKind.EMERGENCY_ISOLATION_ROLLBACK: ("EMERGENCY_ISOLATION_ROLLED_BACK", IsolationRollbackResult),
    GovernedCommandKind.AGENT_DRAFT_SAVE: ("AGENT_DRAFT_SAVED", AgentDraftResult),
    GovernedCommandKind.AGENT_PROMOTE: ("AGENT_PROMOTED", AgentPromotionResult),
    GovernedCommandKind.AGENT_EVALUATE: ("AGENT_EVALUATED", AgentEvaluationResult),
    GovernedCommandKind.AGENT_ROLLBACK: ("AGENT_ROLLED_BACK", AgentRollbackResult),
    GovernedCommandKind.AGENT_KILL_SWITCH: ("AGENT_KILL_SWITCH_ACTIVATED", AgentKillSwitchResult),
    GovernedCommandKind.AGENT_EVALUATION_CERT_SIGN: ("AGENT_EVALUATION_CERT_SIGNED", AgentCertificateResult),
    GovernedCommandKind.SAFETY_SIMULATE: ("SAFETY_SIMULATED", SafetySimulationResult),
    GovernedCommandKind.DRIFT_RAW_EVIDENCE_REQUEST: ("DRIFT_RAW_EVIDENCE_GRANTED", DriftEvidenceAccessResult),
    GovernedCommandKind.EVALUATION_REPORT_EXPORT: ("EVALUATION_REPORT_EXPORTED", EvaluationReportResult),
    GovernedCommandKind.SAFETY_GUARDRAIL_ENFORCE: ("SAFETY_GUARDRAIL_ENFORCED", SafetyGuardrailResult),
    GovernedCommandKind.SAFETY_CANARY_APPROVE: ("SAFETY_CANARY_APPROVED", SafetyCanaryResult),
    GovernedCommandKind.INCIDENT_WAR_ROOM_OPEN: ("INCIDENT_WAR_ROOM_OPENED", IncidentWarRoomResult),
    GovernedCommandKind.INCIDENT_REPORT_EXPORT: ("INCIDENT_REPORT_EXPORTED", IncidentReportResult),
    GovernedCommandKind.INCIDENT_VALIDATION_RUN: ("INCIDENT_VALIDATION_COMPLETED", IncidentValidationResult),
    GovernedCommandKind.BACKLOG_TICKET_OPEN: ("BACKLOG_TICKET_OPENED", BacklogTicketResult),
    GovernedCommandKind.OUTCOME_EXPORT: ("OUTCOME_EXPORTED", OutcomeExportResult),
}

for _kind, _outcome in {
    GovernedCommandKind.CONNECTOR_CREATE: "CONNECTOR_CREATED",
    GovernedCommandKind.CONNECTOR_PROBE: "CONNECTOR_PROBED",
    GovernedCommandKind.CONNECTOR_SYNC: "CONNECTOR_SYNCED",
    GovernedCommandKind.CONNECTOR_REINDEX: "CONNECTOR_REINDEXED",
    GovernedCommandKind.CONNECTOR_SECRET_ROTATE: "CONNECTOR_SECRET_ROTATED",
    GovernedCommandKind.CONNECTOR_SCOPE_REDUCE: "CONNECTOR_SCOPE_REDUCED",
    GovernedCommandKind.CONNECTOR_REVOKE: "CONNECTOR_REVOKED",
    GovernedCommandKind.CONNECTOR_DELETE: "CONNECTOR_DELETED",
    GovernedCommandKind.CONNECTOR_OAUTH_REAUTHORIZE: "CONNECTOR_OAUTH_REAUTHORIZED",
    GovernedCommandKind.CONNECTOR_PAUSE: "CONNECTOR_PAUSED",
    GovernedCommandKind.CONNECTOR_QUARANTINE: "CONNECTOR_QUARANTINED",
    GovernedCommandKind.CONNECTOR_DRIFT_HEAL: "CONNECTOR_DRIFT_HEALED",
    GovernedCommandKind.CONNECTOR_KILL_SWITCH: "CONNECTOR_KILL_SWITCH_ACTIVATED",
}.items():
    GOVERNED_EXTERNAL_RESULTS[_kind] = (_outcome, ConnectorOperationResult)

GOVERNED_EXTERNAL_RESULTS.update({
    GovernedCommandKind.DATASET_IMPORT: ("DATASET_IMPORTED", DatasetImportResult),
    GovernedCommandKind.EVALUATION_COMPARE: ("EVALUATION_COMPARED", EvaluationComparisonResult),
    GovernedCommandKind.EVALUATION_RUN: ("EVALUATION_COMPLETED", EvaluationExecutionResult),
    GovernedCommandKind.EVALUATION_RERUN: ("EVALUATION_RERUN_COMPLETED", EvaluationExecutionResult),
    GovernedCommandKind.EVALUATION_GATE_APPROVE: ("EVALUATION_GATE_REQUESTED", EvaluationGateResult),
})

for _kind, _outcome in {
    GovernedCommandKind.INCIDENT_EMERGENCY_STOP: "INCIDENT_TRAFFIC_STOPPED",
    GovernedCommandKind.INCIDENT_CONNECTOR_REAUTH: "INCIDENT_CONNECTOR_REAUTHORIZED",
    GovernedCommandKind.INCIDENT_SAFE_ROLLBACK: "INCIDENT_ROLLBACK_COMPLETED",
    GovernedCommandKind.INCIDENT_RECOVERY_RESYNC: "INCIDENT_RESYNC_COMPLETED",
    GovernedCommandKind.INCIDENT_SKIP_QUARANTINED: "INCIDENT_QUARANTINED_RUNS_SKIPPED",
    GovernedCommandKind.INCIDENT_ROUTINE_PAUSE: "INCIDENT_ROUTINE_PAUSED",
    GovernedCommandKind.INCIDENT_CONTAIN: "INCIDENT_CONTAINED",
    GovernedCommandKind.RUN_QUARANTINE: "INCIDENT_RUNS_QUARANTINED",
    GovernedCommandKind.RUN_REPLAY: "INCIDENT_RUNS_REPLAYED",
    GovernedCommandKind.RUN_COMPENSATE: "INCIDENT_RUNS_COMPENSATED",
    GovernedCommandKind.INCIDENT_RECOVERY: "INCIDENT_RECOVERED",
}.items():
    GOVERNED_EXTERNAL_RESULTS[_kind] = (_outcome, IncidentOperationResult)
