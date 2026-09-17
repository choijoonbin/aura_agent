from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from dwp_agent.admin_control_plane_contracts import (
    CreateGovernedCommandRequest,
    GovernedCommandObservation,
    GovernedCommandState,
    ModelsRoutingSnapshot,
)
from dwp_agent.admin_control_plane_errors import AdminControlPlaneDenied
from dwp_agent.admin_control_plane_store import (
    _allowed_transitions,
    _require_independent_checker,
)


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


def test_admin_command_requires_a_bound_recovery_plan_hash_and_echoes_review_fields() -> None:
    request = CreateGovernedCommandRequest.model_validate(_create_payload())

    assert request.preflight.recovery_plan.startswith("Restore")
    assert request.model_dump(by_alias=True)["ticketRef"] == "AI-2401"

    invalid = _create_payload()
    invalid["preflight"] = {**invalid["preflight"], "recoveryPlanHash": "0" * 64}  # type: ignore[index]
    with pytest.raises(ValidationError, match="recoveryPlanHash"):
        CreateGovernedCommandRequest.model_validate(invalid)


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

    observation = GovernedCommandObservation.model_validate({
        "commandId": str(uuid4()), "expectedVersion": 2,
        "state": "SUCCEEDED", "progressPercent": 100,
        "resultSummary": "Routing policy version 4 applied.",
        "domainReceiptRef": "ai-control:receipt-44",
        "resultSnapshot": {"policyVersion": 4}, "resultVersion": 4,
    })
    assert observation.state == GovernedCommandState.SUCCEEDED


def test_incomplete_worker_state_requires_safe_problem() -> None:
    with pytest.raises(ValidationError, match="safe problem"):
        GovernedCommandObservation.model_validate({
            "commandId": str(uuid4()), "expectedVersion": 2, "state": "PARTIAL",
        })


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
