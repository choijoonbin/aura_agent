from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import httpx
from pydantic import ValidationError

from dwp_agent.admin_control_plane_adapters import (
    AdminCommandExecutionContext,
    AdminCommandExecutionRejected,
    AdminCommandAdapterResponse,
    HttpAdminCommandAdapter,
    admin_command_capabilities,
)
from dwp_agent.admin_control_plane_contracts import (
    CreateGovernedCommandRequest,
    GovernedCommandDecisionRequest,
    GovernedCommandKind,
    GovernedCommandObservation,
    GovernedCommandState,
    GovernedCommandTransitionRequest,
    ModelsRoutingSnapshot,
    TokenBudgetSummary,
)
from dwp_agent.admin_control_plane_registry import (
    ADMIN_COMMAND_REGISTRY,
    AdminCommandExecutionMode,
    command_spec,
)
from dwp_agent.admin_control_plane_executor import PostgresAdminControlCommandExecutor
from dwp_agent.admin_control_plane_result_validation import (
    require_external_admin_result_contract,
)
from dwp_agent.canonical_json import canonical_json_bytes
from dwp_agent.admin_control_plane_errors import (
    AdminControlPlaneConflict,
    AdminControlPlaneDenied,
)
from dwp_agent.admin_control_plane_worker import AdminControlPlaneWorkerStore
from dwp_agent.admin_external_result_contracts import GOVERNED_EXTERNAL_RESULTS
from dwp_agent.admin_control_plane_store import (
    _allowed_transitions,
    _require_independent_checker,
)
from dwp_agent.governed_worker_runtime import (
    register_governed_worker_heartbeat,
    remove_governed_worker_heartbeat,
)
from dwp_agent.main import app


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


def _create_payload() -> dict[str, object]:
    recovery = "Restore the previous immutable routing policy snapshot."
    return {
        "commandId": str(uuid4()),
        "kind": "MODEL_ROUTING_UPDATE",
        "target": {"type": "MODEL_ROUTING", "id": "ASK_RUNTIME"},
        "expectedVersion": 3,
        "reason": "Move the approved workload to the evaluated route.",
        "ticketRef": "AI-2401",
        "evidenceRefs": ["eval:run-41"],
        "impactAcknowledged": True,
        "preflight": {
            "changes": [{"field": "Primary route", "before": "model-a", "after": "model-b"}],
            "impactScopes": ["ASK_RUNTIME"],
            "recoveryPlan": recovery,
            "recoveryPlanHash": hashlib.sha256(recovery.encode()).hexdigest(),
        },
        "payload": {"primaryModelId": "model-b"},
    }


def _execution_context(
    kind: GovernedCommandKind,
    *,
    target_type: str = "CONNECTOR",
    target_id: str = "connector-1",
    expected_version: int = 3,
    payload: dict[str, object] | None = None,
) -> AdminCommandExecutionContext:
    return AdminCommandExecutionContext(
        command_id=uuid4(),
        attempt_id=uuid4(),
        tenant_id=42,
        maker_user_id="maker-user",
        correlation_id="correlation-1",
        kind=kind,
        state=GovernedCommandState.RUNNING,
        command_revision=2,
        target_type=target_type,
        target_id=target_id,
        expected_target_version=expected_version,
        payload=payload or {},
        review={"ticketRef": "AI-2401"},
        rollback_requested=False,
        rollback_source_receipt_ref=None,
    )


def _adapter_receipt(
    context: AdminCommandExecutionContext,
    snapshot: dict[str, object],
    **updates: object,
) -> AdminCommandAdapterResponse:
    spec = command_spec(context.kind)
    result_version = (
        context.expected_target_version + 1
        if spec.resource_strategy.value == "AI_POLICY"
        else 1
        if spec.result_resource_type is not None or spec.resource_strategy.value == "RESULT"
        else context.expected_target_version + 1
    )
    values: dict[str, object] = {
        "command_id": context.command_id,
        "tenant_id": context.tenant_id,
        "correlation_id": context.correlation_id,
        "attempt_id": context.attempt_id,
        "kind": context.kind,
        "target": {"type": context.target_type, "id": context.target_id},
        "expected_version": context.expected_target_version,
        "state": GovernedCommandState.SUCCEEDED,
        "result_summary": "The provider completed the governed command.",
        "domain_receipt_ref": f"provider:receipt:{context.command_id}",
        "result_snapshot": snapshot,
        "result_version": result_version,
        "result_sha256": hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest(),
    }
    values.update(updates)
    return AdminCommandAdapterResponse.model_validate(values)


def _external_result_envelope(
    context: AdminCommandExecutionContext,
    *,
    result_version: int = 1,
) -> dict[str, object]:
    resource_type, resource_id = command_spec(context.kind).output_resource(
        target_type=context.target_type,
        target_id=context.target_id,
        command_id=str(context.command_id),
    )
    return {
        "resourceType": resource_type,
        "resourceId": resource_id,
        "commandId": str(context.command_id),
        "resultVersion": result_version,
        "state": "COMPLETED",
        "data": {"ticketId": "ticket-1"},
    }


def _connector_snapshot(context: AdminCommandExecutionContext) -> dict[str, object]:
    return {
        "connectorId": context.target_id,
        "name": "Verified connector",
        "providerType": "SHAREPOINT",
        "ownerRef": "team:knowledge",
        "tenantScope": "tenant:42",
        "region": "kr-central",
        "repositories": ["repository-1"],
        "health": "HEALTHY",
        "syncState": "IDLE",
        "aclCoverage": 100,
        "lastSuccessfulSyncAt": "2026-09-17T00:00:00Z",
        "secretExpiresAt": None,
        "version": context.expected_target_version + 1,
    }


def _governed_external_fixture(
    kind: GovernedCommandKind,
) -> tuple[AdminCommandExecutionContext, dict[str, object]]:
    now = "2026-09-17T00:00:00Z"
    later = "2026-09-18T00:00:00Z"
    if kind in _CONNECTOR_EFFECTS:
        expected_version = 0 if kind == GovernedCommandKind.CONNECTOR_CREATE else 3
        payload = {"connectorId": "connector-1", "operation": kind.value}
        context = _execution_context(
            kind, target_type="CONNECTOR", target_id="connector-1",
            expected_version=expected_version, payload=payload,
        )
        return context, {
            "connectorId": "connector-1", "connectorVersion": expected_version + 1,
            "effect": _CONNECTOR_EFFECTS[kind], "appliedPayload": payload,
            "providerOperationId": f"operation:{kind.value.lower()}",
            "effectReceiptId": f"provider:receipt:{context.command_id}",
            "completedAt": now,
        }
    if kind == GovernedCommandKind.DATASET_IMPORT:
        payload = {
            "name": "Release evaluation", "ownerRef": "team:safety", "format": "CSV",
            "checksumSha256": "a" * 64, "schemaMapping": "prompt,response,expected",
            "piiHandling": "QUARANTINE_AND_REVIEW",
        }
        context = _execution_context(
            kind, target_type="EVALUATION_DATASET", target_id="dataset-1",
            expected_version=0, payload=payload,
        )
        return context, {
            "datasetId": "dataset-1", "datasetVersion": 1, "importId": "import-1",
            **payload, "piiState": "PENDING", "importedCaseCount": 12,
            "completedAt": now,
        }
    if kind == GovernedCommandKind.EVALUATION_COMPARE:
        payload = {
            "datasetId": "dataset-1", "baseline": "baseline-v1",
            "candidate": "candidate-v2", "promptVersion": "prompt-v3",
            "policyVersion": "policy-v4", "toolVersion": "tools-v5",
            "evaluatorVersion": "evaluator-v6",
        }
        context = _execution_context(
            kind, target_type="EVALUATION_DATASET", target_id="dataset-1",
            expected_version=3, payload=payload,
        )
        return context, {
            "comparisonId": "comparison-1", "datasetId": "dataset-1",
            "datasetVersion": 3, **{key: value for key, value in payload.items()
                                    if key != "datasetId"},
            "state": "COMPLETED", "evidenceDigest": "b" * 64,
            "completedAt": now,
        }
    if kind in {GovernedCommandKind.EVALUATION_RUN, GovernedCommandKind.EVALUATION_RERUN}:
        payload = {"datasetId": "dataset-1", "datasetVersion": 3}
        if kind == GovernedCommandKind.EVALUATION_RUN:
            payload["pinned"] = True
        else:
            payload.update({"comparisonId": "comparison-1", "preservePinnedVersions": True})
        context = _execution_context(
            kind, target_type="EVALUATION_DATASET", target_id="dataset-1",
            expected_version=3, payload=payload,
        )
        return context, {
            "runId": "evaluation-run-1", "datasetId": "dataset-1", "datasetVersion": 3,
            "pinned": payload.get("pinned"), "comparisonId": "comparison-1",
            "preservePinnedVersions": payload.get("preservePinnedVersions"),
            "state": "COMPLETED", "resultArtifactSha256": "c" * 64,
            "evidenceDigest": "d" * 64, "completedAt": now,
        }
    if kind == GovernedCommandKind.EVALUATION_GATE_APPROVE:
        payload = {
            "comparisonId": "comparison-1", "datasetChecksum": "e" * 64,
            "decision": "REQUEST_APPROVAL",
        }
        context = _execution_context(
            kind, target_type="EVALUATION_DATASET", target_id="dataset-1",
            expected_version=3, payload=payload,
        )
        return context, {
            "datasetId": "dataset-1", "datasetVersion": 3,
            **payload, "gateState": "APPROVAL_REQUESTED",
            "approvalReceiptId": "approval:1", "completedAt": now,
        }
    if kind in _INCIDENT_EFFECTS:
        payload = {
            "incidentId": "incident-1", "correlationId": "incident-correlation-1",
            "operation": {"kind": kind.value, "mode": "VERIFIED"},
        }
        context = _execution_context(
            kind, target_type="AI_INCIDENT", target_id="incident-1",
            expected_version=3, payload=payload,
        )
        return context, {
            "incidentId": "incident-1", "incidentVersion": 4,
            "correlationId": "incident-correlation-1", "effect": _INCIDENT_EFFECTS[kind],
            "appliedPayload": payload, "providerOperationId": f"operation:{kind.value.lower()}",
            "effectReceiptId": f"provider:receipt:{context.command_id}", "completedAt": now,
        }
    target_type, target_id = "AGENT_REVISION", "agent-revision-1"
    payload: dict[str, object] = {"request": "bound"}
    results: dict[GovernedCommandKind, dict[str, object]] = {
        GovernedCommandKind.MODEL_CANARY_START: {
            "policyId": "policy-1", "basePolicyVersion": 3, "appliedPolicyVersion": 4,
            "trafficPercent": 5, "primaryModelId": "model-a", "fallbackModelIds": ["model-b"],
            "canaryState": "STARTED", "startedAt": now, "expiresAt": later,
            "evidenceRef": "evidence:canary", "budgetMode": "BLOCK", "dailyBudget": 100,
            "modalities": ["TEXT"], "agentScopes": ["ASK_RUNTIME"],
            "inFlightPolicy": "DRAIN",
        },
        GovernedCommandKind.MODEL_ROLLBACK: {
            "policyId": "policy-1", "fromVersion": 3, "restoredVersion": 2,
            "activePrimaryModelId": "model-a", "activeFallbackModelIds": ["model-b"],
            "rollbackOfReceiptId": "receipt:prior", "inFlightPolicy": "DRAIN",
            "state": "ROLLED_BACK",
        },
        GovernedCommandKind.PROVIDER_CIRCUIT_BREAK: {
            "providerId": "provider-1", "circuitState": "OPEN", "inFlightPolicy": "MIGRATE",
            "fallbackRouteIds": ["route-b"], "effectiveAt": now, "evidenceRef": "evidence:circuit",
        },
        GovernedCommandKind.MODEL_SMART_ISOLATE: {
            "modelId": "model-1", "servingState": "ISOLATED", "fallbackModelIds": ["model-b"],
            "inFlightOutcome": "MIGRATED", "effectiveAt": now,
        },
        GovernedCommandKind.EMERGENCY_ISOLATION_ROLLBACK: {
            "routingScope": "ASK_RUNTIME", "policyId": "policy-1",
            "isolationState": "RESTORED", "validationStatus": "PASS",
            "validationReceiptId": "validation:1", "validationRequired": True,
        },
        GovernedCommandKind.AGENT_DRAFT_SAVE: {
            "agentRevisionId": "agent-revision-1", "baseVersion": 3, "savedVersion": 4,
            "draftSha256": "a" * 64, "lifecycle": "DRAFT_SAVED",
        },
        GovernedCommandKind.AGENT_PROMOTE: {
            "agentRevisionId": "agent-revision-1", "priorLifecycle": "DRAFT",
            "lifecycle": "CANARY", "rolloutPercent": 5,
            "evaluationEvidenceRef": "evaluation:1", "appliedVersion": 4,
        },
        GovernedCommandKind.AGENT_EVALUATE: {
            "agentRevisionId": "agent-revision-1", "evaluationRunId": "evaluation:1",
            "pinnedDraftSha256": "b" * 64, "suiteId": "suite-1", "suiteVersion": "v1",
            "outcome": "PASS", "metrics": {"accuracy": 0.99}, "evidenceDigest": "c" * 64,
        },
        GovernedCommandKind.AGENT_ROLLBACK: {
            "agentRevisionId": "agent-revision-1", "fromVersion": 3,
            "restoredRevision": "agent-revision-0", "restoredVersion": 2,
            "rollbackOfReceiptId": "receipt:prior", "lifecycle": "ACTIVE",
        },
        GovernedCommandKind.AGENT_KILL_SWITCH: {
            "agentRevisionId": "agent-revision-1", "lifecycle": "DISABLED",
            "inFlightHandling": "CANCEL", "effectiveAt": now, "evidenceRef": "evidence:kill",
        },
        GovernedCommandKind.AGENT_EVALUATION_CERT_SIGN: {
            "agentRevisionId": "agent-revision-1", "evaluationRunId": "evaluation:1",
            "certificateId": "certificate-1", "attestationDigest": "d" * 64,
            "keyId": "key-1", "algorithm": "ED25519", "signature": "signed-value",
        },
        GovernedCommandKind.SAFETY_SIMULATE: {
            "simulationId": "simulation-1", "safetyPolicyId": "production",
            "suites": ["PROMPT_INJECTION"], "perSuiteVerdicts": {"PROMPT_INJECTION": "PASS"},
            "overallVerdict": "PASS", "sampleClassification": "ANONYMIZED",
            "resultArtifactSha256": "e" * 64, "evidenceDigest": "f" * 64,
        },
        GovernedCommandKind.DRIFT_RAW_EVIDENCE_REQUEST: {
            "signalId": "signal-1", "accessGrantId": "grant-1", "permission": "TIME_BOUND_READ",
            "state": "GRANTED", "expiresAt": later, "evidenceRef": "evidence:grant",
        },
        GovernedCommandKind.EVALUATION_REPORT_EXPORT: {
            "exportId": "export-1", "artifactId": "artifact-1", "datasetId": "dataset-1",
            "datasetVersion": 3, "comparisonId": "comparison-1", "format": "PDF",
            "includeEvidence": True, "contentSha256": "1" * 64, "byteLength": 100,
            "expiresAt": later,
        },
        GovernedCommandKind.SAFETY_GUARDRAIL_ENFORCE: {
            "scope": "ASK_RUNTIME", "guardrailPolicyVersion": 4,
            "enforcementState": "ENFORCED", "inFlightPolicy": "DRAIN", "failClosed": True,
            "effectiveAt": now, "evidenceRef": "evidence:guardrail",
        },
        GovernedCommandKind.SAFETY_CANARY_APPROVE: {
            "scope": "ASK_RUNTIME", "canaryId": "canary-1", "trafficPercent": 5,
            "durationMinutes": 30, "startsAt": now, "expiresAt": later, "autoStop": True,
            "state": "APPROVED", "evaluationEvidenceRef": "evaluation:1",
        },
        GovernedCommandKind.INCIDENT_WAR_ROOM_OPEN: {
            "incidentId": "incident-1", "correlationId": "incident-correlation-1",
            "warRoomId": "war-room-1", "warRoomRef": "collaboration:1",
            "participantScope": ["security"], "timelineBound": True, "createdAt": now,
        },
        GovernedCommandKind.INCIDENT_REPORT_EXPORT: {
            "exportId": "export-1", "incidentId": "incident-1",
            "correlationId": "incident-correlation-1", "incidentVersion": 3,
            "formats": ["PDF"], "includeTimeline": True,
            "artifacts": [{"format": "PDF", "id": "artifact-1", "contentSha256": "2" * 64,
                           "byteLength": 100, "expiresAt": later}],
        },
        GovernedCommandKind.INCIDENT_VALIDATION_RUN: {
            "validationRunId": "validation-1", "incidentId": "incident-1",
            "correlationId": "incident-correlation-1", "incidentVersion": 3,
            "checks": [{"name": "provider-health", "outcome": "PASS", "evidenceRef": "e:1"}],
            "canaryPercent": 5, "reQuarantineOnFailure": True,
            "overallOutcome": "PASS", "completedAt": now,
        },
        GovernedCommandKind.BACKLOG_TICKET_OPEN: {
            "itemId": "backlog-1", "sourceVersion": 3, "ticketId": "ticket-1",
            "ticketSystem": "JIRA", "ticketRef": "AI-1", "ticketState": "OPEN",
            "title": "Improve retrieval", "ownerTeam": "knowledge", "priority": "P1",
            "metricEvidence": "metric:1", "targetValue": "95%", "createdAt": now,
        },
        GovernedCommandKind.OUTCOME_EXPORT: {
            "exportId": "export-1", "periodDays": 30, "organization": "ALL",
            "workType": "ALL", "privacyThreshold": 5, "format": "CSV",
            "suppressedCohortCount": 1, "rowCount": 10, "contentSha256": "3" * 64,
            "byteLength": 100, "expiresAt": later, "downloadRef": "download:1",
        },
    }
    target_overrides = {
        GovernedCommandKind.MODEL_CANARY_START: ("ROUTING_POLICY", "policy-1"),
        GovernedCommandKind.MODEL_ROLLBACK: ("ROUTING_POLICY", "policy-1"),
        GovernedCommandKind.PROVIDER_CIRCUIT_BREAK: ("PROVIDER", "provider-1"),
        GovernedCommandKind.MODEL_SMART_ISOLATE: ("MODEL", "model-1"),
        GovernedCommandKind.EMERGENCY_ISOLATION_ROLLBACK: ("ROUTING_SCOPE", "ASK_RUNTIME"),
        GovernedCommandKind.SAFETY_SIMULATE: ("SAFETY_POLICY", "production"),
        GovernedCommandKind.DRIFT_RAW_EVIDENCE_REQUEST: ("DRIFT_SIGNAL", "signal-1"),
        GovernedCommandKind.EVALUATION_REPORT_EXPORT: ("EVALUATION_DATASET", "dataset-1"),
        GovernedCommandKind.SAFETY_GUARDRAIL_ENFORCE: ("SAFETY_SCOPE", "ASK_RUNTIME"),
        GovernedCommandKind.SAFETY_CANARY_APPROVE: ("SAFETY_SCOPE", "ASK_RUNTIME"),
        GovernedCommandKind.INCIDENT_WAR_ROOM_OPEN: ("AI_INCIDENT", "incident-1"),
        GovernedCommandKind.INCIDENT_REPORT_EXPORT: ("AI_INCIDENT", "incident-1"),
        GovernedCommandKind.INCIDENT_VALIDATION_RUN: ("AI_INCIDENT", "incident-1"),
        GovernedCommandKind.BACKLOG_TICKET_OPEN: ("IMPROVEMENT_BACKLOG", "backlog-1"),
        GovernedCommandKind.OUTCOME_EXPORT: ("OUTCOME_AGGREGATE", "period-30"),
    }
    target_type, target_id = target_overrides.get(kind, (target_type, target_id))
    payload = {
        GovernedCommandKind.MODEL_CANARY_START: {
            "primaryModelId": "model-a", "fallbackModelIds": ["model-b"],
            "trafficPercent": 5, "budgetMode": "BLOCK", "dailyBudget": 100,
            "modalities": ["TEXT"], "agentScopes": ["ASK_RUNTIME"],
            "inFlightPolicy": "DRAIN",
        },
        GovernedCommandKind.MODEL_ROLLBACK: {
            "primaryModelId": "model-a", "fallbackModelIds": ["model-b"],
            "inFlightPolicy": "DRAIN",
        },
        GovernedCommandKind.PROVIDER_CIRCUIT_BREAK: {
            "providerId": "provider-1", "inFlightPolicy": "MIGRATE",
        },
        GovernedCommandKind.MODEL_SMART_ISOLATE: {
            "modelId": "model-1", "fallbackModelIds": ["model-b"],
        },
        GovernedCommandKind.EMERGENCY_ISOLATION_ROLLBACK: {
            "policyId": "policy-1", "validationRequired": True,
        },
        GovernedCommandKind.AGENT_DRAFT_SAVE: {"allowedWork": "Draft agent"},
        GovernedCommandKind.AGENT_PROMOTE: {
            "rolloutPercent": 5, "evaluationEvidence": "evaluation:1",
        },
        GovernedCommandKind.AGENT_EVALUATE: {
            "suiteId": "suite-1", "suiteVersion": "v1", "draft": "pinned",
        },
        GovernedCommandKind.AGENT_EVALUATION_CERT_SIGN: {
            "evaluationRunId": "evaluation:1", "evaluationEvidenceDigest": "d" * 64,
        },
        GovernedCommandKind.AGENT_ROLLBACK: {"rollbackSourceRef": "receipt:prior"},
        GovernedCommandKind.AGENT_KILL_SWITCH: {"inFlightHandling": "CANCEL"},
        GovernedCommandKind.SAFETY_SIMULATE: {"suites": ["PROMPT_INJECTION"]},
        GovernedCommandKind.DRIFT_RAW_EVIDENCE_REQUEST: {
            "signalId": "signal-1", "access": "TIME_BOUND_READ",
        },
        GovernedCommandKind.EVALUATION_REPORT_EXPORT: {
            "comparisonId": "comparison-1", "format": "PDF", "includeEvidence": True,
        },
        GovernedCommandKind.SAFETY_GUARDRAIL_ENFORCE: {
            "scope": "ASK_RUNTIME", "inFlightPolicy": "DRAIN", "failClosed": True,
        },
        GovernedCommandKind.SAFETY_CANARY_APPROVE: {
            "scope": "ASK_RUNTIME", "trafficPercent": 5,
            "durationMinutes": 30, "autoStop": True,
        },
        GovernedCommandKind.INCIDENT_WAR_ROOM_OPEN: {
            "correlationId": "incident-correlation-1", "participantScope": ["security"],
            "bindTimeline": True,
        },
        GovernedCommandKind.INCIDENT_REPORT_EXPORT: {
            "correlationId": "incident-correlation-1", "formats": ["PDF"],
            "includeTimeline": True,
        },
        GovernedCommandKind.INCIDENT_VALIDATION_RUN: {
            "correlationId": "incident-correlation-1", "checks": ["provider-health"],
            "canaryPercent": 5, "reQuarantineOnFailure": True,
        },
        GovernedCommandKind.BACKLOG_TICKET_OPEN: {
            "itemId": "backlog-1", "title": "Improve retrieval", "ownerTeam": "knowledge",
            "priority": "P1", "metricEvidence": "metric:1", "targetValue": "95%",
        },
        GovernedCommandKind.OUTCOME_EXPORT: {
            "periodDays": 30, "organization": "ALL", "workType": "ALL",
            "privacyThreshold": 5, "format": "CSV",
        },
    }[kind]
    if kind == GovernedCommandKind.AGENT_DRAFT_SAVE:
        results[kind]["draftSha256"] = hashlib.sha256(
            canonical_json_bytes(payload)
        ).hexdigest()
    if kind == GovernedCommandKind.AGENT_EVALUATE:
        results[kind]["pinnedDraftSha256"] = hashlib.sha256(
            canonical_json_bytes(payload)
        ).hexdigest()
    context = _execution_context(
        kind, target_type=target_type, target_id=target_id,
        expected_version=3, payload=payload,
    )
    return context, results[kind]


def _governed_external_snapshot(
    context: AdminCommandExecutionContext, result: dict[str, object],
) -> dict[str, object]:
    spec = command_spec(context.kind)
    result_version = (
        context.expected_target_version + 1
        if spec.resource_strategy.value == "AI_POLICY"
        else 1
        if spec.result_resource_type is not None or spec.resource_strategy.value == "RESULT"
        else context.expected_target_version + 1
    )
    resource_type, resource_id = spec.output_resource(
        target_type=context.target_type, target_id=context.target_id,
        command_id=str(context.command_id),
    )
    outcome, _ = GOVERNED_EXTERNAL_RESULTS[context.kind]
    return {
        "schemaVersion": 1, "commandId": str(context.command_id),
        "attemptId": str(context.attempt_id), "tenantId": context.tenant_id,
        "correlationId": context.correlation_id, "kind": context.kind.value,
        "target": {"type": context.target_type, "id": context.target_id},
        "expectedVersion": context.expected_target_version,
        "requestPayloadSha256": hashlib.sha256(
            canonical_json_bytes(context.payload)
        ).hexdigest(),
        "resourceType": resource_type, "resourceId": resource_id,
        "resultVersion": result_version, "state": "COMPLETED", "outcome": outcome,
        "providerReceiptId": f"provider:receipt:{context.command_id}",
        "completedAt": datetime.now(UTC).isoformat(), "evidenceRefs": ["evidence:provider"],
        "result": result,
        "resultSha256": hashlib.sha256(canonical_json_bytes(result)).hexdigest(),
    }


def test_admin_command_requires_a_bound_recovery_plan_hash_and_echoes_review_fields() -> None:
    request = CreateGovernedCommandRequest.model_validate(_create_payload())

    assert request.preflight.recovery_plan.startswith("Restore")
    assert request.model_dump(by_alias=True)["ticketRef"] == "AI-2401"

    invalid = _create_payload()
    invalid["preflight"] = {**invalid["preflight"], "recoveryPlanHash": "0" * 64}  # type: ignore[index]
    with pytest.raises(ValidationError, match="recoveryPlanHash"):
        CreateGovernedCommandRequest.model_validate(invalid)


def test_admin_audit_inputs_normalize_and_reject_whitespace_or_empty_evidence() -> None:
    normalized = _create_payload()
    normalized.update({
        "reason": "  Move the approved workload safely.  ",
        "ticketRef": "  AI-2401  ",
        "evidenceRefs": ["  eval:run-41  "],
    })
    normalized["preflight"] = {
        **normalized["preflight"],  # type: ignore[dict-item]
        "changes": [{"field": "  Primary route  ", "before": "a", "after": "b"}],
        "impactScopes": ["  ASK_RUNTIME  "],
    }
    request = CreateGovernedCommandRequest.model_validate(normalized)
    assert request.reason == "Move the approved workload safely."
    assert request.ticket_ref == "AI-2401"
    assert request.evidence_refs == ["eval:run-41"]
    assert request.preflight.changes[0].field == "Primary route"
    assert request.preflight.impact_scopes == ["ASK_RUNTIME"]

    for field, value, message in (
        ("reason", "     ", "reason"),
        ("ticketRef", "   ", "ticketRef"),
        ("evidenceRefs", [], "at least one"),
        ("evidenceRefs", ["   "], "evidenceRefs item"),
        ("evidenceRefs", ["e" * 241], "at most 240"),
        ("evidenceRefs", ["evidence:a", " evidence:a "], "unique"),
    ):
        invalid = _create_payload()
        invalid[field] = value
        with pytest.raises(ValidationError, match=message):
            CreateGovernedCommandRequest.model_validate(invalid)

    invalid_preflights = [
        (
            {
                "changes": [{"field": "   ", "before": "a", "after": "b"}],
            },
            "changes.field",
        ),
        ({"impactScopes": ["   "]}, "impactScopes item"),
        ({"impactScopes": ["ASK_RUNTIME", " ASK_RUNTIME "]}, "unique"),
    ]
    for update, message in invalid_preflights:
        invalid = _create_payload()
        invalid["preflight"] = {**invalid["preflight"], **update}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match=message):
            CreateGovernedCommandRequest.model_validate(invalid)

    whitespace_plan = "     "
    invalid = _create_payload()
    invalid["preflight"] = {
        **invalid["preflight"],  # type: ignore[dict-item]
        "recoveryPlan": whitespace_plan,
        "recoveryPlanHash": hashlib.sha256(whitespace_plan.encode()).hexdigest(),
    }
    with pytest.raises(ValidationError, match="recoveryPlan"):
        CreateGovernedCommandRequest.model_validate(invalid)

    decision_payload = {
        "commandId": str(uuid4()),
        "decision": "APPROVE",
        "expectedVersion": 1,
        "reason": "Independent checker approval.",
        "evidenceRefs": ["evidence:checker"],
    }
    transition_payload = {
        "commandId": str(uuid4()),
        "expectedVersion": 1,
        "reason": "Cancel after verified review.",
        "evidenceRefs": ["evidence:cancel"],
    }
    for model, payload in (
        (GovernedCommandDecisionRequest, decision_payload),
        (GovernedCommandTransitionRequest, transition_payload),
    ):
        with pytest.raises(ValidationError, match="reason"):
            model.model_validate({**payload, "reason": "     "})
        with pytest.raises(ValidationError, match="at least one"):
            model.model_validate({**payload, "evidenceRefs": []})
        with pytest.raises(ValidationError, match="unique"):
            model.model_validate(
                {**payload, "evidenceRefs": ["evidence:a", " evidence:a "]}
            )


def test_maker_cannot_approve_and_independent_checker_can() -> None:
    transitions, reason = _allowed_transitions(
        GovernedCommandState.AWAITING_APPROVAL, "maker-1", "maker-1"
    )
    assert transitions == ["CANCEL"]
    assert reason and "self-approval" in reason
    with pytest.raises(AdminControlPlaneDenied, match="self-approval"):
        _require_independent_checker("maker-1", "maker-1")

    transitions, reason = _allowed_transitions(
        GovernedCommandState.AWAITING_APPROVAL, "maker-1", "checker-2"
    )
    assert transitions == ["APPROVE", "REJECT", "CANCEL"]
    assert reason is None
    _require_independent_checker("maker-1", "checker-2")


def test_worker_cannot_claim_success_without_domain_receipt_and_versioned_snapshot() -> None:
    with pytest.raises(ValidationError, match="domain receipt"):
        GovernedCommandObservation.model_validate({
            "commandId": str(uuid4()), "expectedVersion": 2,
            "state": "SUCCEEDED", "resultSummary": "done",
        })

    snapshot = {"policyVersion": 4}
    observation = GovernedCommandObservation.model_validate({
        "commandId": str(uuid4()), "attemptId": str(uuid4()), "expectedVersion": 2,
        "tenantId": 42, "correlationId": "correlation-1",
        "state": "SUCCEEDED", "progressPercent": 100,
        "resultSummary": "Routing policy version 4 applied.",
        "domainReceiptRef": "ai-control:receipt-44",
        "resultSnapshot": snapshot, "resultVersion": 4,
        "resultSha256": hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest(),
    })
    assert observation.state == GovernedCommandState.SUCCEEDED

    for update in (
        {"resultSummary": "   "},
        {"domainReceiptRef": "   "},
        {"resultSnapshot": {}},
        {"resultSha256": "0" * 64},
    ):
        with pytest.raises(ValidationError):
            GovernedCommandObservation.model_validate({
                **observation.model_dump(mode="json", by_alias=True), **update,
            })


def test_internal_worker_observation_api_rejects_unbound_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ADMIN_CONTROL_WORKER_TOKEN", "w" * 32)
    command_id = uuid4()
    snapshot = {"connectorId": "connector-1"}
    body = {
        "commandId": str(uuid4()), "attemptId": str(uuid4()), "expectedVersion": 2,
        "tenantId": 42, "correlationId": "correlation-1",
        "state": "SUCCEEDED", "resultSummary": "   ",
        "domainReceiptRef": "provider:receipt:1", "resultSnapshot": snapshot,
        "resultVersion": 4,
        "resultSha256": hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest(),
    }

    async def post(payload: dict[str, object]) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/internal/v1/admin/control-plane/commands/{command_id}/observations",
                json=payload,
                headers={
                    "X-DWP-Admin-Control-Worker-Token": "w" * 32,
                    "X-DWP-Tenant-ID": "42", "X-DWP-Worker-ID": "worker-1",
                    "X-Correlation-ID": "correlation-1",
                },
            )

    assert asyncio.run(post(body)).status_code == 422
    assert asyncio.run(post({**body, "resultSummary": "done", "resultSha256": "0" * 64})).status_code == 422


def test_incomplete_worker_state_requires_safe_problem() -> None:
    with pytest.raises(ValidationError, match="safe problem"):
        GovernedCommandObservation.model_validate({
            "commandId": str(uuid4()), "expectedVersion": 2, "state": "PARTIAL",
        })


def test_external_adapter_success_receipt_is_bound_to_complete_command_context() -> None:
    context, result = _governed_external_fixture(GovernedCommandKind.CONNECTOR_PROBE)
    snapshot = _governed_external_snapshot(context, result)
    receipt = _adapter_receipt(context, snapshot)
    PostgresAdminControlCommandExecutor._verify_adapter_receipt(context, receipt)

    mutations = (
        {"tenant_id": context.tenant_id + 1},
        {"correlation_id": "correlation-other"},
        {"attempt_id": uuid4()},
        {"kind": GovernedCommandKind.MODEL_CANARY_START},
        {"target": {"type": "CONNECTOR", "id": "connector-other"}},
        {"expected_version": context.expected_target_version + 1},
        {"result_sha256": "0" * 64},
    )
    for mutation in mutations:
        with pytest.raises(AdminCommandExecutionRejected, match="not bound|digest"):
            PostgresAdminControlCommandExecutor._verify_adapter_receipt(
                context, _adapter_receipt(context, snapshot, **mutation)
            )
    with pytest.raises(AdminCommandExecutionRejected, match="typed result"):
        PostgresAdminControlCommandExecutor._verify_adapter_receipt(
            context, _adapter_receipt(context, {})
        )
    governed_external = _execution_context(
        GovernedCommandKind.AGENT_DRAFT_SAVE,
        target_type="AGENT",
        target_id="agent-1",
    )
    require_external_admin_result_contract(
        governed_external, command_spec(governed_external.kind)
    )
    with pytest.raises(AdminCommandExecutionRejected, match="typed result"):
        PostgresAdminControlCommandExecutor._verify_adapter_receipt(
            governed_external,
            _adapter_receipt(
                governed_external, _external_result_envelope(governed_external)
            ),
        )
    policy_external = _execution_context(
        GovernedCommandKind.MODEL_CANARY_START,
        target_type="MODEL_ROUTING",
        target_id="ASK_RUNTIME",
    )
    require_external_admin_result_contract(
        policy_external, command_spec(policy_external.kind)
    )
    for field in ("result_summary", "domain_receipt_ref"):
        with pytest.raises(ValidationError, match="context-bound receipt"):
            _adapter_receipt(context, snapshot, **{field: "   "})


@pytest.mark.parametrize("kind", tuple(GOVERNED_EXTERNAL_RESULTS))
def test_each_governed_external_kind_requires_typed_payload_bound_result(
    kind: GovernedCommandKind,
) -> None:
    context, typed_result = _governed_external_fixture(kind)
    snapshot = _governed_external_snapshot(context, typed_result)
    PostgresAdminControlCommandExecutor._verify_adapter_receipt(
        context, _adapter_receipt(context, snapshot)
    )

    invalid = {**snapshot, "requestPayloadSha256": "0" * 64}
    with pytest.raises(AdminCommandExecutionRejected, match="command context"):
        PostgresAdminControlCommandExecutor._verify_adapter_receipt(
            context, _adapter_receipt(context, invalid)
        )

    effect_mutations: dict[GovernedCommandKind, tuple[str, object]] = {
        GovernedCommandKind.MODEL_CANARY_START: ("primaryModelId", "model-other"),
        GovernedCommandKind.MODEL_ROLLBACK: ("inFlightPolicy", "CANCEL"),
        GovernedCommandKind.PROVIDER_CIRCUIT_BREAK: ("inFlightPolicy", "CANCEL"),
        GovernedCommandKind.MODEL_SMART_ISOLATE: ("fallbackModelIds", ["model-other"]),
        GovernedCommandKind.EMERGENCY_ISOLATION_ROLLBACK: ("validationRequired", False),
        GovernedCommandKind.AGENT_DRAFT_SAVE: ("draftSha256", "0" * 64),
        GovernedCommandKind.AGENT_PROMOTE: ("rolloutPercent", 25),
        GovernedCommandKind.AGENT_EVALUATE: ("suiteId", "suite-other"),
        GovernedCommandKind.AGENT_EVALUATION_CERT_SIGN: ("evaluationRunId", "evaluation:other"),
        GovernedCommandKind.AGENT_ROLLBACK: ("rollbackOfReceiptId", "receipt:other"),
        GovernedCommandKind.AGENT_KILL_SWITCH: ("inFlightHandling", "DRAIN"),
        GovernedCommandKind.SAFETY_SIMULATE: ("suites", ["TOOL_MISUSE"]),
        GovernedCommandKind.DRIFT_RAW_EVIDENCE_REQUEST: ("permission", "OTHER"),
        GovernedCommandKind.EVALUATION_REPORT_EXPORT: ("format", "JSONL"),
        GovernedCommandKind.SAFETY_GUARDRAIL_ENFORCE: ("failClosed", False),
        GovernedCommandKind.SAFETY_CANARY_APPROVE: ("trafficPercent", 25),
        GovernedCommandKind.INCIDENT_WAR_ROOM_OPEN: ("participantScope", ["other"]),
        GovernedCommandKind.INCIDENT_REPORT_EXPORT: ("formats", ["JSONL"]),
        GovernedCommandKind.INCIDENT_VALIDATION_RUN: ("canaryPercent", 25),
        GovernedCommandKind.BACKLOG_TICKET_OPEN: ("title", "Different ticket"),
        GovernedCommandKind.OUTCOME_EXPORT: ("privacyThreshold", 99),
    }
    if kind in _CONNECTOR_EFFECTS or kind in _INCIDENT_EFFECTS:
        field, value = "effect", "UNRELATED_EFFECT"
    elif kind == GovernedCommandKind.DATASET_IMPORT:
        field, value = "checksumSha256", "0" * 64
    elif kind == GovernedCommandKind.EVALUATION_COMPARE:
        field, value = "baseline", "baseline-other"
    elif kind in {GovernedCommandKind.EVALUATION_RUN, GovernedCommandKind.EVALUATION_RERUN}:
        field, value = "datasetVersion", 2
    elif kind == GovernedCommandKind.EVALUATION_GATE_APPROVE:
        field, value = "datasetChecksum", "0" * 64
    else:
        field, value = effect_mutations[kind]
    wrong_effect = {**typed_result, field: value}
    with pytest.raises(
        AdminCommandExecutionRejected,
        match="typed result|effect|dataset|comparison|evaluation",
    ):
        wrong_snapshot = _governed_external_snapshot(context, wrong_effect)
        PostgresAdminControlCommandExecutor._verify_adapter_receipt(
            context, _adapter_receipt(context, wrong_snapshot)
        )


def test_governed_external_rollback_binds_the_reversed_receipt() -> None:
    base, typed_result = _governed_external_fixture(GovernedCommandKind.AGENT_PROMOTE)
    context = replace(
        base, rollback_requested=True,
        rollback_source_receipt_ref="provider:receipt:prior",
    )
    snapshot = _governed_external_snapshot(context, typed_result)
    rollback_result = {
        "rollbackOfReceiptId": "provider:receipt:prior",
        "restoredResourceVersion": 4,
        "rollbackEvidenceDigest": "a" * 64,
        "state": "ROLLED_BACK",
    }
    snapshot.update({
        "state": "ROLLED_BACK", "outcome": "COMMAND_ROLLED_BACK",
        "result": rollback_result,
        "resultSha256": hashlib.sha256(
            canonical_json_bytes(rollback_result)
        ).hexdigest(),
    })
    receipt = _adapter_receipt(
        context, snapshot, state=GovernedCommandState.ROLLED_BACK,
        rollback_ref="provider:receipt:prior",
    )
    PostgresAdminControlCommandExecutor._verify_adapter_receipt(context, receipt)

    wrong = {**rollback_result, "rollbackOfReceiptId": "provider:receipt:other"}
    wrong_snapshot = {
        **snapshot, "result": wrong,
        "resultSha256": hashlib.sha256(canonical_json_bytes(wrong)).hexdigest(),
    }
    with pytest.raises(AdminCommandExecutionRejected, match="receipt it reverses"):
        PostgresAdminControlCommandExecutor._verify_adapter_receipt(
            context,
            _adapter_receipt(
                context, wrong_snapshot, state=GovernedCommandState.ROLLED_BACK,
                rollback_ref="provider:receipt:prior",
            ),
        )
    wrong_version = {**rollback_result, "restoredResourceVersion": 2}
    wrong_version_snapshot = {
        **snapshot, "result": wrong_version,
        "resultSha256": hashlib.sha256(
            canonical_json_bytes(wrong_version)
        ).hexdigest(),
    }
    with pytest.raises(AdminCommandExecutionRejected, match="receipt it reverses"):
        PostgresAdminControlCommandExecutor._verify_adapter_receipt(
            context,
            _adapter_receipt(
                context, wrong_version_snapshot,
                state=GovernedCommandState.ROLLED_BACK,
                rollback_ref="provider:receipt:prior",
            ),
        )


def test_http_admin_adapter_rejects_redirects_and_oversized_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ADMIN_CONTROL_CONNECTOR_URL", "http://localhost/command")
    monkeypatch.setenv("DWP_ADMIN_CONTROL_CONNECTOR_TOKEN", "t" * 32)
    context = _execution_context(GovernedCommandKind.CONNECTOR_PROBE)
    spec = command_spec(context.kind)

    redirect = HttpAdminCommandAdapter(
        "connector",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "http://localhost/other"})
        ),
    )
    with pytest.raises(AdminCommandExecutionRejected, match="HTTP 302"):
        redirect.execute(service="connector", context=context, spec=spec)

    oversized = HttpAdminCommandAdapter(
        "connector",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 257)),
        maximum_response_bytes=256,
    )
    with pytest.raises(AdminCommandExecutionRejected, match="response size"):
        oversized.execute(service="connector", context=context, spec=spec)

    context, result = _governed_external_fixture(GovernedCommandKind.CONNECTOR_PROBE)
    valid_receipt = _adapter_receipt(
        context, _governed_external_snapshot(context, result)
    )
    valid = HttpAdminCommandAdapter(
        "connector",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=valid_receipt.model_dump(mode="json", by_alias=True)
            )
        ),
    ).execute(service="connector", context=context, spec=spec)
    PostgresAdminControlCommandExecutor._verify_adapter_receipt(context, valid)


def test_https_admin_adapter_requires_an_exact_common_host_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DWP_ADMIN_CONTROL_TICKETING_URL",
        "https://ticketing-broker.internal/v1/commands",
    )
    monkeypatch.setenv("DWP_ADMIN_CONTROL_TICKETING_TOKEN", "t" * 32)
    monkeypatch.delenv("DWP_ADMIN_CONTROL_ALLOWED_HOSTS", raising=False)
    assert HttpAdminCommandAdapter("ticketing").configured is False

    monkeypatch.setenv("DWP_ADMIN_CONTROL_ALLOWED_HOSTS", "other.internal")
    assert HttpAdminCommandAdapter("ticketing").configured is False

    monkeypatch.setenv(
        "DWP_ADMIN_CONTROL_ALLOWED_HOSTS",
        "other.internal, ticketing-broker.internal",
    )
    assert HttpAdminCommandAdapter("ticketing").configured is True

    monkeypatch.setenv(
        "DWP_ADMIN_CONTROL_AGENT_REGISTRY_URL",
        "https://ticketing-broker.internal/v1/agents",
    )
    monkeypatch.setenv("DWP_ADMIN_CONTROL_AGENT_REGISTRY_TOKEN", "a" * 32)
    monkeypatch.setenv(
        "DWP_ADMIN_CONTROL_CONNECTOR_URL",
        "https://ticketing-broker.internal/v1/connectors",
    )
    monkeypatch.setenv("DWP_ADMIN_CONTROL_CONNECTOR_TOKEN", "c" * 32)
    register_governed_worker_heartbeat("ADMIN_CONTROL_COMMAND")
    try:
        capabilities = admin_command_capabilities()
    finally:
        remove_governed_worker_heartbeat("ADMIN_CONTROL_COMMAND")
    by_kind = {item.kind: item for item in capabilities.commands}
    assert by_kind[GovernedCommandKind.AGENT_DRAFT_SAVE].status.value == "AVAILABLE"
    assert by_kind[GovernedCommandKind.CONNECTOR_PROBE].status.value == "AVAILABLE"


def test_evaluation_success_receipt_requires_typed_dataset_binding() -> None:
    context, result = _governed_external_fixture(GovernedCommandKind.EVALUATION_RUN)
    snapshot = _governed_external_snapshot(context, result)
    PostgresAdminControlCommandExecutor._verify_adapter_receipt(
        context, _adapter_receipt(context, snapshot)
    )

    for invalid_result in (
        {**result, "state": "ACCEPTED"},
        {**result, "datasetId": "dataset-other"},
        {**result, "datasetVersion": 2},
    ):
        with pytest.raises(AdminCommandExecutionRejected, match="typed result|evaluation"):
            PostgresAdminControlCommandExecutor._verify_adapter_receipt(
                context,
                _adapter_receipt(
                    context, _governed_external_snapshot(context, invalid_result)
                ),
            )

    rerun, rerun_result = _governed_external_fixture(
        GovernedCommandKind.EVALUATION_RERUN
    )
    with pytest.raises(AdminCommandExecutionRejected, match="typed result|evaluation"):
        PostgresAdminControlCommandExecutor._verify_adapter_receipt(
            rerun,
            _adapter_receipt(
                rerun,
                _governed_external_snapshot(
                    rerun, {**rerun_result, "comparisonId": "comparison-other"}
                ),
            ),
        )


def test_external_dataset_and_incident_results_require_target_context_binding() -> None:
    dataset, dataset_result = _governed_external_fixture(
        GovernedCommandKind.DATASET_IMPORT
    )
    dataset_snapshot = _governed_external_snapshot(dataset, dataset_result)
    PostgresAdminControlCommandExecutor._verify_adapter_receipt(
        dataset, _adapter_receipt(dataset, dataset_snapshot)
    )
    with pytest.raises(AdminCommandExecutionRejected, match="typed result|dataset"):
        PostgresAdminControlCommandExecutor._verify_adapter_receipt(
            dataset,
            _adapter_receipt(
                dataset,
                _governed_external_snapshot(
                    dataset, {**dataset_result, "datasetId": "dataset-other"}
                ),
            ),
        )

    incident, incident_result = _governed_external_fixture(
        GovernedCommandKind.INCIDENT_CONTAIN
    )
    incident_snapshot = _governed_external_snapshot(incident, incident_result)
    PostgresAdminControlCommandExecutor._verify_adapter_receipt(
        incident, _adapter_receipt(incident, incident_snapshot)
    )
    for changed in (
        {"incidentId": "incident-other"},
        {"correlationId": "incident-correlation-other"},
    ):
        with pytest.raises(AdminCommandExecutionRejected, match="incident"):
            PostgresAdminControlCommandExecutor._verify_adapter_receipt(
                incident,
                _adapter_receipt(
                    incident,
                    _governed_external_snapshot(
                        incident, {**incident_result, **changed}
                    ),
                ),
            )


def test_model_route_simulation_input_and_snapshot_references_fail_closed() -> None:
    request = _create_payload()
    request["kind"] = "MODEL_ROUTE_SIMULATE"
    request["payload"] = {
        "requesterRole": "KNOWLEDGE_WORKER", "agentId": "DWP_ASSISTANT",
        "dataClassification": "INTERNAL", "estimatedTokens": 4_096,
        "modality": "TEXT", "constraints": ["KR_REGION"],
    }
    assert CreateGovernedCommandRequest.model_validate(request).payload["estimatedTokens"] == 4_096
    request["payload"] = {"agentId": "DWP_ASSISTANT"}
    with pytest.raises(ValidationError):
        CreateGovernedCommandRequest.model_validate(request)

    now = datetime.now(UTC)
    snapshot = {
        "generatedAt": now, "capability": {"status": "AVAILABLE", "configured": True},
        "providers": [{"providerId": "provider-a", "name": "Provider A", "kind": "MANAGED",
                       "region": "kr-central", "health": "HEALTHY", "activeModelCount": 1,
                       "updatedAt": now}],
        "models": [{"modelId": "provider-a:model-a", "providerId": "provider-a",
                    "displayName": "Model A", "modalities": ["TEXT"], "lifecycle": "ACTIVE",
                    "allowedDataClassifications": ["INTERNAL"], "governancePolicy": "policy-v3",
                    "region": "kr-central", "credentialState": "BOUND", "credentialRef": "credential:model-a"}],
        "routingPolicies": [{"policyId": "ASK_RUNTIME", "name": "ASK", "scope": "ASK_RUNTIME",
                             "primaryModelId": "provider-a:model-a", "fallbackModelIds": [],
                             "budgetMode": "BLOCK", "version": 3, "state": "ACTIVE", "updatedAt": now}],
        "routingRules": [{"ruleId": "rule-a", "name": "Internal text", "taskType": "ASK",
                          "conditions": ["modality=TEXT"], "allowedDataClassifications": ["INTERNAL"],
                          "primaryModelId": "provider-a:model-a", "fallbackModelIds": [],
                          "failClosed": True, "version": 3}],
        "latestSimulation": {"simulationId": "simulation-a", "decision": "ROUTED",
                             "matchedRuleId": "rule-a", "targetModelId": "provider-a:model-a",
                             "estimatedCost": None, "currency": "USD", "estimatedLatencyMs": None,
                             "fallbackModelIds": [], "generatedAt": now},
        "pendingApprovalCount": 0, "activeCanaryCount": 0,
        "emergencyStopActive": False, "monthlySpend": None, "monthlyBudget": None,
    }
    assert ModelsRoutingSnapshot.model_validate(snapshot).latest_simulation is not None
    snapshot["routingRules"][0]["primaryModelId"] = "missing:model"  # type: ignore[index]
    with pytest.raises(ValidationError, match="reference models"):
        ModelsRoutingSnapshot.model_validate(snapshot)


def test_budget_and_evaluation_commands_validate_runtime_enforcement_inputs() -> None:
    budget = _create_payload()
    budget.update({
        "kind": "TOKEN_BUDGET_UPDATE",
        "target": {"type": "TOKEN_BUDGET", "id": "ASK_RUNTIME"},
        "payload": {"budgetTokens": 25_000, "policyMode": "THROTTLE"},
    })
    assert CreateGovernedCommandRequest.model_validate(budget).payload["policyMode"] == "THROTTLE"

    budget_snapshot = {
        "scope": "ASK_RUNTIME",
        "consumedTokens": 2_000,
        "budgetTokens": 25_000,
        "projectedTokens": 3_000,
        "spikeDetected": False,
        "policyMode": "BLOCK",
        "enforcementActivationState": "DISABLED",
        "version": 4,
    }
    assert (
        TokenBudgetSummary.model_validate(budget_snapshot).enforcement_activation_state.value
        == "DISABLED"
    )
    with pytest.raises(ValidationError, match="enforcementActivationState"):
        TokenBudgetSummary.model_validate(
            {**budget_snapshot, "enforcementActivationState": "UNKNOWN"}
        )

    invalid_budget = {**budget, "payload": {"budgetTokens": 25_000, "policyMode": "DISPLAY_ONLY"}}
    with pytest.raises(ValidationError, match="policyMode"):
        CreateGovernedCommandRequest.model_validate(invalid_budget)

    pii = _create_payload()
    pii.update({
        "kind": "DATASET_PII_DECIDE",
        "target": {"type": "EVALUATION_DATASET", "id": "dataset-1"},
        "payload": {
            "decision": "PASS",
            "evidenceRefs": ["evidence:pii-review"],
            "reviewerNote": "Verified the redaction and retention evidence.",
        },
    })
    assert CreateGovernedCommandRequest.model_validate(pii).payload["decision"] == "PASS"
    with pytest.raises(ValidationError, match="evidence"):
        CreateGovernedCommandRequest.model_validate({**pii, "payload": {**pii["payload"], "evidenceRefs": []}})

    comparison = _create_payload()
    comparison.update({
        "kind": "EVALUATION_COMPARE",
        "target": {"type": "EVALUATION_DATASET", "id": "dataset-1"},
        "payload": {
            "datasetId": "dataset-1",
            "baseline": "baseline-v1",
            "candidate": "candidate-v2",
            "promptVersion": "prompt-v3",
            "policyVersion": "policy-v4",
            "toolVersion": "tools-v5",
            "evaluatorVersion": "evaluator-v6",
        },
    })
    assert CreateGovernedCommandRequest.model_validate(comparison).target.id == "dataset-1"

    evaluation_run = _create_payload()
    evaluation_run.update({
        "kind": "EVALUATION_RUN",
        "target": {"type": "EVALUATION_DATASET", "id": "dataset-1"},
        "expectedVersion": 7,
        "payload": {
            "datasetId": "dataset-1",
            "datasetVersion": 7,
            "pinned": True,
        },
    })
    validated_run = CreateGovernedCommandRequest.model_validate(evaluation_run)
    assert validated_run.expected_version == validated_run.payload["datasetVersion"]

    evaluation_rerun = {
        **evaluation_run,
        "kind": "EVALUATION_RERUN",
        "payload": {
            **evaluation_run["payload"],  # type: ignore[arg-type]
            "comparisonId": "comparison-1",
            "preservePinnedVersions": True,
        },
    }
    validated_rerun = CreateGovernedCommandRequest.model_validate(evaluation_rerun)
    assert validated_rerun.target.type == "EVALUATION_DATASET"
    assert validated_rerun.payload["comparisonId"] == "comparison-1"

    invalid_rerun_target = {
        **evaluation_rerun,
        "target": {"type": "EVALUATION_COMPARISON", "id": "comparison-1"},
    }
    with pytest.raises(ValidationError, match="evaluation dataset target"):
        CreateGovernedCommandRequest.model_validate(invalid_rerun_target)

    missing_comparison = {
        **evaluation_rerun,
        "payload": {
            "datasetId": "dataset-1",
            "datasetVersion": 7,
            "preservePinnedVersions": True,
        },
    }
    with pytest.raises(ValidationError, match="comparisonId"):
        CreateGovernedCommandRequest.model_validate(missing_comparison)

    stale_dataset_version = {
        **evaluation_run,
        "payload": {**evaluation_run["payload"], "datasetVersion": 6},  # type: ignore[arg-type]
    }
    with pytest.raises(ValidationError, match="match expectedVersion"):
        CreateGovernedCommandRequest.model_validate(stale_dataset_version)


def test_admin_command_registry_and_capabilities_cover_all_65_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_services = {
        spec.service for spec in ADMIN_COMMAND_REGISTRY.values() if spec.service
    }
    for service in external_services:
        suffix = service.upper().replace("-", "_")
        monkeypatch.delenv(f"DWP_ADMIN_CONTROL_{suffix}_URL", raising=False)
        monkeypatch.delenv(f"DWP_ADMIN_CONTROL_{suffix}_TOKEN", raising=False)
    register_governed_worker_heartbeat("ADMIN_CONTROL_COMMAND")
    try:
        snapshot = admin_command_capabilities()
    finally:
        remove_governed_worker_heartbeat("ADMIN_CONTROL_COMMAND")

    assert len(ADMIN_COMMAND_REGISTRY) == len(GovernedCommandKind) == 65
    assert set(ADMIN_COMMAND_REGISTRY) == set(GovernedCommandKind)
    external_kinds = {
        kind
        for kind, spec in ADMIN_COMMAND_REGISTRY.items()
        if spec.mode == AdminCommandExecutionMode.EXTERNAL_ADAPTER
    }
    assert len(external_kinds) == 50
    assert external_kinds == set(GOVERNED_EXTERNAL_RESULTS)
    assert {spec.family for spec in ADMIN_COMMAND_REGISTRY.values()} == {
        "A01", "A02", "A03", "A04", "A05", "A06",
    }
    assert len(snapshot.commands) == 65
    by_kind = {entry.kind: entry for entry in snapshot.commands}
    for kind, spec in ADMIN_COMMAND_REGISTRY.items():
        expected = (
            "AVAILABLE"
            if spec.mode == AdminCommandExecutionMode.INTERNAL
            else "NOT_CONFIGURED"
        )
        assert by_kind[kind].status.value == expected
        assert by_kind[kind].configured is (
            spec.mode == AdminCommandExecutionMode.INTERNAL
        )


def test_external_adapter_contract_never_accepts_unverified_success() -> None:
    context = _execution_context(GovernedCommandKind.CONNECTOR_PROBE)
    binding = {
        "commandId": str(context.command_id),
        "tenantId": context.tenant_id,
        "correlationId": context.correlation_id,
        "attemptId": str(context.attempt_id),
        "kind": context.kind.value,
        "target": {"type": context.target_type, "id": context.target_id},
        "expectedVersion": context.expected_target_version,
    }
    with pytest.raises(ValidationError, match="receipt and versioned snapshot"):
        AdminCommandAdapterResponse.model_validate({
            **binding,
            "state": "SUCCEEDED",
            "resultSummary": "claimed success without domain evidence",
        })

    with pytest.raises(ValidationError, match="safe problem"):
        AdminCommandAdapterResponse.model_validate({
            **binding,
            "state": "FAILED",
        })


def test_worker_rejects_external_success_without_a_typed_result_contract() -> None:
    command_id = uuid4()
    snapshot = {
        "resourceType": "BACKLOG_TICKET",
        "resourceId": str(command_id),
        "commandId": str(command_id),
        "resultVersion": 1,
        "state": "COMPLETED",
        "data": {"fabricated": True},
    }
    observation = GovernedCommandObservation.model_validate({
        "commandId": str(uuid4()), "attemptId": str(uuid4()),
        "expectedVersion": 2,
        "tenantId": 42,
        "correlationId": "correlation-1",
        "state": "SUCCEEDED",
        "progressPercent": 100,
        "resultSummary": "Ticket provider claimed completion.",
        "domainReceiptRef": "ticket:receipt:1",
        "resultSnapshot": snapshot,
        "resultVersion": 1,
        "resultSha256": hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest(),
    })
    store = object.__new__(AdminControlPlaneWorkerStore)
    store.public_store = type(
        "FakePublicStore", (), {"_decrypt": staticmethod(lambda *_: {})}
    )()
    with pytest.raises(AdminControlPlaneConflict, match="result contract"):
        store._validate_result_snapshot(
            {
                "tenant_id": 42,
                "target_type": "IMPROVEMENT_BACKLOG",
                "target_id": "backlog-1",
                "command_id": command_id,
                "maker_user_id": "maker-user",
                "correlation_id": "correlation-1",
                "revision": 2,
                "expected_target_version": 2,
                "payload_envelope": "encrypted",
            },
            observation,
            command_spec(GovernedCommandKind.BACKLOG_TICKET_OPEN),
        )

    rollback_snapshot = {"tombstoned": True}
    rollback = GovernedCommandObservation.model_validate({
        "commandId": str(uuid4()), "attemptId": str(uuid4()), "expectedVersion": 2,
        "tenantId": 42, "correlationId": "correlation-1",
        "state": "ROLLED_BACK", "resultSummary": "Provider claimed rollback.",
        "domainReceiptRef": "ticket:receipt:rollback", "rollbackRef": "ticket:receipt:1",
        "resultSnapshot": rollback_snapshot, "resultVersion": 1,
        "resultSha256": hashlib.sha256(
            canonical_json_bytes(rollback_snapshot)
        ).hexdigest(),
    })
    with pytest.raises(AdminControlPlaneConflict, match="result contract"):
        store._validate_result_snapshot(
            {
                "tenant_id": 42, "target_type": "IMPROVEMENT_BACKLOG",
                "target_id": "backlog-1", "command_id": command_id,
                "maker_user_id": "maker-user", "correlation_id": "correlation-1",
                "revision": 2, "expected_target_version": 2,
                "payload_envelope": "encrypted",
            },
            rollback,
            command_spec(GovernedCommandKind.BACKLOG_TICKET_OPEN),
        )
