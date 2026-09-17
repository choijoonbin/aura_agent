from __future__ import annotations

import hashlib
from typing import Any

from .admin_control_plane_adapters import (
    AdminCommandExecutionContext,
    AdminCommandExecutionRejected,
)
from .admin_control_plane_contracts import GovernedCommandKind
from .canonical_json import canonical_json_bytes


_CONNECTOR_EFFECTS = {
    GovernedCommandKind.CONNECTOR_CREATE: "CREATED",
    GovernedCommandKind.CONNECTOR_PROBE: "PROBE_COMPLETED",
    GovernedCommandKind.CONNECTOR_SYNC: "SYNC_COMPLETED",
    GovernedCommandKind.CONNECTOR_REINDEX: "REINDEX_COMPLETED",
    GovernedCommandKind.CONNECTOR_SECRET_ROTATE: "SECRET_ROTATED",
    GovernedCommandKind.CONNECTOR_SCOPE_REDUCE: "SCOPE_REDUCED",
    GovernedCommandKind.CONNECTOR_REVOKE: "REVOKED",
    GovernedCommandKind.CONNECTOR_DELETE: "DELETED",
    GovernedCommandKind.CONNECTOR_OAUTH_REAUTHORIZE: "OAUTH_REAUTHORIZED",
    GovernedCommandKind.CONNECTOR_PAUSE: "PAUSED",
    GovernedCommandKind.CONNECTOR_QUARANTINE: "QUARANTINED",
    GovernedCommandKind.CONNECTOR_DRIFT_HEAL: "DRIFT_HEALED",
    GovernedCommandKind.CONNECTOR_KILL_SWITCH: "KILL_SWITCH_ACTIVATED",
}

_INCIDENT_EFFECTS = {
    GovernedCommandKind.INCIDENT_EMERGENCY_STOP: "TRAFFIC_STOPPED",
    GovernedCommandKind.INCIDENT_CONNECTOR_REAUTH: "CONNECTOR_REAUTHORIZED",
    GovernedCommandKind.INCIDENT_SAFE_ROLLBACK: "ROLLBACK_COMPLETED",
    GovernedCommandKind.INCIDENT_RECOVERY_RESYNC: "RESYNC_COMPLETED",
    GovernedCommandKind.INCIDENT_SKIP_QUARANTINED: "QUARANTINED_RUNS_SKIPPED",
    GovernedCommandKind.INCIDENT_ROUTINE_PAUSE: "ROUTINE_PAUSED",
    GovernedCommandKind.INCIDENT_CONTAIN: "CONTAINED",
    GovernedCommandKind.RUN_QUARANTINE: "RUNS_QUARANTINED",
    GovernedCommandKind.RUN_REPLAY: "RUNS_REPLAYED",
    GovernedCommandKind.RUN_COMPENSATE: "RUNS_COMPENSATED",
    GovernedCommandKind.INCIDENT_RECOVERY: "RECOVERED",
}


def validate_governed_effect_binding(
    context: AdminCommandExecutionContext,
    result: Any,
    result_version: int,
    provider_receipt_id: str,
) -> None:
    kind = context.kind
    payload = context.payload
    if kind in _CONNECTOR_EFFECTS:
        if not (
            result.connector_id == context.target_id
            and result.connector_version == result_version
            and result.effect == _CONNECTOR_EFFECTS[kind]
            and result.applied_payload == payload
            and result.effect_receipt_id == provider_receipt_id
        ):
            _invalid("The connector effect does not match the reviewed command.")
        return
    if kind == GovernedCommandKind.DATASET_IMPORT:
        if not (
            result.dataset_id == context.target_id
            and result.dataset_version == result_version
            and result.name == payload.get("name")
            and result.owner_ref == payload.get("ownerRef")
            and result.format == payload.get("format")
            and result.checksum_sha256 == payload.get("checksumSha256")
            and result.schema_mapping == payload.get("schemaMapping")
            and result.pii_handling == payload.get("piiHandling")
        ):
            _invalid("The imported dataset does not match the reviewed import.")
        return
    if kind == GovernedCommandKind.EVALUATION_COMPARE:
        fields = (
            ("dataset_id", "datasetId"), ("baseline", "baseline"),
            ("candidate", "candidate"), ("prompt_version", "promptVersion"),
            ("policy_version", "policyVersion"), ("tool_version", "toolVersion"),
            ("evaluator_version", "evaluatorVersion"),
        )
        if not (
            result.dataset_version == context.expected_target_version
            and all(getattr(result, field) == payload.get(key) for field, key in fields)
        ):
            _invalid("The completed comparison does not match its pinned inputs.")
        return
    if kind in {GovernedCommandKind.EVALUATION_RUN, GovernedCommandKind.EVALUATION_RERUN}:
        matches = (
            result.dataset_id == context.target_id
            and result.dataset_id == payload.get("datasetId")
            and result.dataset_version == context.expected_target_version
            and result.dataset_version == payload.get("datasetVersion")
        )
        if kind == GovernedCommandKind.EVALUATION_RUN:
            matches = matches and result.pinned == payload.get("pinned")
        else:
            matches = (
                matches
                and result.comparison_id == payload.get("comparisonId")
                and result.preserve_pinned_versions == payload.get("preservePinnedVersions")
            )
        if not matches:
            _invalid("The completed evaluation does not match the reviewed dataset and pins.")
        return
    if kind == GovernedCommandKind.EVALUATION_GATE_APPROVE:
        if not (
            result.dataset_id == context.target_id
            and result.dataset_version == context.expected_target_version
            and result.comparison_id == payload.get("comparisonId")
            and result.dataset_checksum == payload.get("datasetChecksum")
            and result.decision == payload.get("decision")
        ):
            _invalid("The release-gate effect does not match the reviewed evidence.")
        return
    if kind in _INCIDENT_EFFECTS:
        if not (
            result.incident_id == context.target_id
            and result.incident_version == result_version
            and result.correlation_id == payload.get("correlationId")
            and result.effect == _INCIDENT_EFFECTS[kind]
            and result.applied_payload == payload
            and result.effect_receipt_id == provider_receipt_id
        ):
            _invalid("The incident effect does not match the reviewed operation.")
        return
    _validate_existing_effect(context, result, result_version)


def _validate_existing_effect(
    context: AdminCommandExecutionContext, result: Any, result_version: int
) -> None:
    kind = context.kind
    target_bound = {
        GovernedCommandKind.MODEL_CANARY_START: "policy_id",
        GovernedCommandKind.MODEL_ROLLBACK: "policy_id",
        GovernedCommandKind.PROVIDER_CIRCUIT_BREAK: "provider_id",
        GovernedCommandKind.MODEL_SMART_ISOLATE: "model_id",
        GovernedCommandKind.EMERGENCY_ISOLATION_ROLLBACK: "routing_scope",
        GovernedCommandKind.AGENT_DRAFT_SAVE: "agent_revision_id",
        GovernedCommandKind.AGENT_PROMOTE: "agent_revision_id",
        GovernedCommandKind.AGENT_EVALUATE: "agent_revision_id",
        GovernedCommandKind.AGENT_ROLLBACK: "agent_revision_id",
        GovernedCommandKind.AGENT_KILL_SWITCH: "agent_revision_id",
        GovernedCommandKind.AGENT_EVALUATION_CERT_SIGN: "agent_revision_id",
        GovernedCommandKind.SAFETY_SIMULATE: "safety_policy_id",
        GovernedCommandKind.DRIFT_RAW_EVIDENCE_REQUEST: "signal_id",
        GovernedCommandKind.EVALUATION_REPORT_EXPORT: "dataset_id",
        GovernedCommandKind.SAFETY_GUARDRAIL_ENFORCE: "scope",
        GovernedCommandKind.SAFETY_CANARY_APPROVE: "scope",
        GovernedCommandKind.INCIDENT_WAR_ROOM_OPEN: "incident_id",
        GovernedCommandKind.INCIDENT_REPORT_EXPORT: "incident_id",
        GovernedCommandKind.INCIDENT_VALIDATION_RUN: "incident_id",
        GovernedCommandKind.BACKLOG_TICKET_OPEN: "item_id",
    }.get(kind)
    if target_bound is not None and getattr(result, target_bound) != context.target_id:
        _invalid("The typed external result does not match the governed target resource.")
    if kind in {
        GovernedCommandKind.INCIDENT_WAR_ROOM_OPEN,
        GovernedCommandKind.INCIDENT_REPORT_EXPORT,
        GovernedCommandKind.INCIDENT_VALIDATION_RUN,
    } and getattr(result, "correlation_id") != context.payload.get("correlationId"):
        _invalid("The typed incident result does not match the reviewed correlation.")
    if kind == GovernedCommandKind.OUTCOME_EXPORT and (
        result.period_days != context.payload.get("periodDays")
        or context.target_id != f"period-{result.period_days}"
    ):
        _invalid("The outcome export result does not match the reviewed period.")
    _validate_existing_payload(context, result, result_version)


def _validate_existing_payload(
    context: AdminCommandExecutionContext, result: Any, result_version: int
) -> None:
    kind = context.kind
    payload = context.payload
    payload_digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    checks = {
        GovernedCommandKind.MODEL_CANARY_START: lambda: result.base_policy_version == context.expected_target_version and result.applied_policy_version == result_version and result.primary_model_id == payload.get("primaryModelId") and result.fallback_model_ids == payload.get("fallbackModelIds") and result.traffic_percent == payload.get("trafficPercent") and result.budget_mode == payload.get("budgetMode") and result.daily_budget == payload.get("dailyBudget") and result.modalities == payload.get("modalities") and result.agent_scopes == payload.get("agentScopes") and result.in_flight_policy == payload.get("inFlightPolicy"),
        GovernedCommandKind.MODEL_ROLLBACK: lambda: result.from_version == context.expected_target_version and result.active_primary_model_id == payload.get("primaryModelId") and result.active_fallback_model_ids == payload.get("fallbackModelIds") and result.in_flight_policy == payload.get("inFlightPolicy"),
        GovernedCommandKind.PROVIDER_CIRCUIT_BREAK: lambda: result.in_flight_policy == payload.get("inFlightPolicy"),
        GovernedCommandKind.MODEL_SMART_ISOLATE: lambda: result.fallback_model_ids == payload.get("fallbackModelIds"),
        GovernedCommandKind.EMERGENCY_ISOLATION_ROLLBACK: lambda: result.policy_id == payload.get("policyId") and result.validation_required == payload.get("validationRequired"),
        GovernedCommandKind.AGENT_DRAFT_SAVE: lambda: result.base_version == context.expected_target_version and result.saved_version == context.expected_target_version + 1 and result.draft_sha256 == payload_digest,
        GovernedCommandKind.AGENT_PROMOTE: lambda: result.rollout_percent == payload.get("rolloutPercent") and result.evaluation_evidence_ref == payload.get("evaluationEvidence") and result.applied_version == result_version,
        GovernedCommandKind.AGENT_EVALUATE: lambda: result.pinned_draft_sha256 == payload_digest and result.suite_id == payload.get("suiteId") and result.suite_version == payload.get("suiteVersion"),
        GovernedCommandKind.AGENT_EVALUATION_CERT_SIGN: lambda: result.evaluation_run_id == payload.get("evaluationRunId"),
        GovernedCommandKind.AGENT_ROLLBACK: lambda: result.from_version == context.expected_target_version and result.rollback_of_receipt_id == payload.get("rollbackSourceRef"),
        GovernedCommandKind.AGENT_KILL_SWITCH: lambda: result.in_flight_handling == payload.get("inFlightHandling"),
        GovernedCommandKind.SAFETY_SIMULATE: lambda: result.suites == payload.get("suites"),
        GovernedCommandKind.DRIFT_RAW_EVIDENCE_REQUEST: lambda: result.permission == payload.get("access"),
        GovernedCommandKind.EVALUATION_REPORT_EXPORT: lambda: result.dataset_version == context.expected_target_version and result.comparison_id == payload.get("comparisonId") and result.format == payload.get("format") and result.include_evidence == payload.get("includeEvidence"),
        GovernedCommandKind.SAFETY_GUARDRAIL_ENFORCE: lambda: result.scope == payload.get("scope") and result.in_flight_policy == payload.get("inFlightPolicy") and result.fail_closed == payload.get("failClosed"),
        GovernedCommandKind.SAFETY_CANARY_APPROVE: lambda: result.scope == payload.get("scope") and result.traffic_percent == payload.get("trafficPercent") and result.duration_minutes == payload.get("durationMinutes") and result.auto_stop == payload.get("autoStop"),
        GovernedCommandKind.INCIDENT_WAR_ROOM_OPEN: lambda: result.participant_scope == payload.get("participantScope") and result.timeline_bound == payload.get("bindTimeline"),
        GovernedCommandKind.INCIDENT_REPORT_EXPORT: lambda: result.incident_version == context.expected_target_version and result.formats == payload.get("formats") and result.include_timeline == payload.get("includeTimeline"),
        GovernedCommandKind.INCIDENT_VALIDATION_RUN: lambda: result.incident_version == context.expected_target_version and [check.name for check in result.checks] == payload.get("checks") and result.canary_percent == payload.get("canaryPercent") and result.re_quarantine_on_failure == payload.get("reQuarantineOnFailure"),
        GovernedCommandKind.BACKLOG_TICKET_OPEN: lambda: result.source_version == context.expected_target_version and result.title == payload.get("title") and result.owner_team == payload.get("ownerTeam") and result.priority == payload.get("priority") and result.metric_evidence == payload.get("metricEvidence") and result.target_value == payload.get("targetValue"),
        GovernedCommandKind.OUTCOME_EXPORT: lambda: result.organization == payload.get("organization") and result.work_type == payload.get("workType") and result.privacy_threshold == payload.get("privacyThreshold") and result.format == payload.get("format"),
    }
    if kind not in checks or not checks[kind]():
        _invalid("The typed external effect does not match the reviewed request payload.")


def _invalid(detail: str) -> None:
    raise AdminCommandExecutionRejected(
        "ADMIN_ADAPTER_RESULT_BINDING_INVALID",
        detail,
        "Reject the result and repair output resource binding in the domain adapter.",
    )
