from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .admin_control_plane_adapters import (
    AdminCommandAdapterResponse,
    AdminCommandExecutionContext,
    AdminCommandExecutionRejected,
)
from .admin_control_plane_contracts import (
    ConnectorSummary,
    DriftSignal,
    EvaluationDatasetSummary,
    GovernedCommandKind,
    GovernedCommandState,
    ImprovementBacklogItem,
    IncidentSummary,
    TokenBudgetSummary,
)
from .admin_control_plane_registry import AdminCommandSpec
from .admin_evaluation_contracts import (
    GovernedEvaluationComparisonResult,
    GovernedEvaluationRunResult,
)
from .admin_model_routing_contracts import LatestRoutingSimulation
from .admin_external_result_contracts import (
    GOVERNED_EXTERNAL_RESULTS,
    GovernedExternalCommandResult,
    GovernedExternalRollbackResult,
)
from .admin_external_result_bindings import validate_governed_effect_binding
from .canonical_json import canonical_json_bytes
from .admin_control_plane_result_support import (
    expected_dynamic_result_type,
    external_result_contract_available,
)


def require_external_admin_result_contract(
    context: AdminCommandExecutionContext, spec: AdminCommandSpec
) -> None:
    if not external_result_contract_available(context.kind, spec):
        _contract_unavailable(context.kind.value)
    expected_dynamic = expected_dynamic_result_type(context.kind, spec)
    if expected_dynamic is not None and context.target_type != expected_dynamic:
        _contract_unavailable(context.kind.value)


def validate_external_admin_result(
    context: AdminCommandExecutionContext,
    response: AdminCommandAdapterResponse,
    spec: AdminCommandSpec,
) -> None:
    snapshot = response.result_snapshot
    if not isinstance(snapshot, dict) or response.result_version is None:
        _invalid("The external result snapshot or version is unavailable.")
    try:
        if context.kind in GOVERNED_EXTERNAL_RESULTS:
            _validate_governed_external_result(context, response, spec)
            return
        _contract_unavailable(context.kind.value)
    except AdminCommandExecutionRejected:
        raise
    except (TypeError, ValueError) as error:
        raise AdminCommandExecutionRejected(
            "ADMIN_ADAPTER_RESULT_INVALID",
            "The external adapter returned an invalid typed result snapshot.",
            "Return the governed output contract bound to the reviewed command and retry.",
        ) from error


def validate_external_admin_observation(
    *, row: Mapping[str, Any], request: Any, spec: AdminCommandSpec,
    payload: dict[str, object],
) -> None:
    context = AdminCommandExecutionContext(
        command_id=row["command_id"], attempt_id=request.attempt_id,
        tenant_id=int(row["tenant_id"]), maker_user_id=row["maker_user_id"],
        correlation_id=row["correlation_id"], kind=spec.kind,
        state=request.state, command_revision=int(row["revision"]),
        target_type=row["target_type"], target_id=row["target_id"],
        expected_target_version=int(row["expected_target_version"]), payload=payload,
        review={}, rollback_requested=request.state == GovernedCommandState.ROLLED_BACK,
        rollback_source_receipt_ref=request.rollback_ref,
    )
    require_external_admin_result_contract(context, spec)
    validate_external_admin_result(
        context,
        AdminCommandAdapterResponse(
            command_id=row["command_id"], tenant_id=int(row["tenant_id"]),
            correlation_id=row["correlation_id"], attempt_id=request.attempt_id,
            kind=spec.kind, target={"type": row["target_type"], "id": row["target_id"]},
            expected_version=int(row["expected_target_version"]), state=request.state,
            result_summary=request.result_summary,
            domain_receipt_ref=request.domain_receipt_ref,
            rollback_ref=request.rollback_ref, result_snapshot=request.result_snapshot,
            result_version=request.result_version, result_sha256=request.result_sha256,
        ),
        spec,
    )


def _validate_evaluation_dataset(context: AdminCommandExecutionContext, result: Any) -> None:
    if not (
        result.command_id == context.command_id
        and result.dataset_id == context.target_id
        and result.dataset_id == context.payload.get("datasetId")
        and result.dataset_version == context.expected_target_version
    ):
        _invalid("The evaluation result does not match the reviewed dataset version.")


def _validate_governed_external_result(
    context: AdminCommandExecutionContext,
    response: AdminCommandAdapterResponse,
    spec: AdminCommandSpec,
) -> None:
    result = GovernedExternalCommandResult.model_validate(response.result_snapshot)
    resource_type, resource_id = spec.output_resource(
        target_type=context.target_type, target_id=context.target_id,
        command_id=str(context.command_id),
    )
    expected_outcome, result_model = GOVERNED_EXTERNAL_RESULTS[context.kind]
    expected_state = (
        "ROLLED_BACK"
        if response.state == GovernedCommandState.ROLLED_BACK
        else "COMPLETED"
    )
    bound = (
        result.command_id == context.command_id
        and result.attempt_id == context.attempt_id
        and result.tenant_id == context.tenant_id
        and result.correlation_id == context.correlation_id
        and result.kind == context.kind
        and result.target.type == context.target_type
        and result.target.id == context.target_id
        and result.expected_version == context.expected_target_version
        and result.request_payload_sha256
        == hashlib.sha256(canonical_json_bytes(context.payload)).hexdigest()
        and result.resource_type == resource_type
        and result.resource_id == resource_id
        and result.result_version == response.result_version
        and result.state == expected_state
        and result.provider_receipt_id == response.domain_receipt_ref
        and result.outcome
        == ("COMMAND_ROLLED_BACK" if expected_state == "ROLLED_BACK" else expected_outcome)
    )
    if not bound:
        _invalid("The typed external result does not match the governed command context.")
    if expected_state == "ROLLED_BACK":
        rollback = GovernedExternalRollbackResult.model_validate(result.result)
        if not (
            rollback.rollback_of_receipt_id == response.rollback_ref
            and rollback.rollback_of_receipt_id == context.rollback_source_receipt_ref
            and rollback.restored_resource_version == result.result_version
        ):
            _invalid("The typed rollback result does not match the receipt it reverses.")
        return
    typed = result_model.model_validate(result.result)
    validate_governed_effect_binding(
        context, typed, result.result_version, result.provider_receipt_id
    )


def _validate_semantic_version(result: Any, receipt_version: int) -> None:
    semantic_version = getattr(result, "result_version", None)
    if semantic_version is None:
        semantic_version = getattr(result, "version", None)
    if semantic_version is not None and semantic_version != receipt_version:
        _invalid("The typed result version does not match its receipt.")


def _validate_resource_identity(
    context: AdminCommandExecutionContext, resource_type: str, result: Any
) -> None:
    if resource_type == "CONNECTOR":
        payload_id = context.payload.get("connectorId")
        if result.connector_id != context.target_id or (
            payload_id is not None and payload_id != context.target_id
        ) or result.tenant_scope != f"tenant:{context.tenant_id}":
            _invalid("The connector result does not match the governed connector target.")
    elif resource_type == "EVALUATION_DATASET":
        payload_id = context.payload.get("datasetId")
        if result.dataset_id != context.target_id or (
            payload_id is not None and payload_id != context.target_id
        ):
            _invalid("The dataset result does not match the governed dataset target.")
    elif resource_type == "AI_INCIDENT":
        if result.incident_id != context.target_id:
            _invalid("The incident result does not match the governed incident target.")
        payload_incident = context.payload.get("incidentId")
        payload_correlation = context.payload.get("correlationId")
        if payload_incident is not None and payload_incident != context.target_id:
            _invalid("The incident payload does not match the governed incident target.")
        if not isinstance(payload_correlation, str) or (
            result.correlation_id != payload_correlation
        ):
            _invalid("The incident result does not match the reviewed correlation.")


def result_validator(resource_type: str) -> Any:
    return _RESULT_VALIDATORS.get(resource_type)


def _invalid(detail: str) -> None:
    raise AdminCommandExecutionRejected(
        "ADMIN_ADAPTER_RESULT_BINDING_INVALID",
        detail,
        "Reject the result and repair output resource binding in the domain adapter.",
    )


def _contract_unavailable(kind: str) -> None:
    raise AdminCommandExecutionRejected(
        "ADMIN_ADAPTER_RESULT_CONTRACT_UNAVAILABLE",
        f"{kind} has no registered typed external result contract.",
        "Implement and register the authoritative result DTO before enabling this command.",
    )


_RESULT_VALIDATORS = {
    "MODEL_ROUTE_SIMULATION": LatestRoutingSimulation,
    "CONNECTOR": ConnectorSummary,
    "EVALUATION_DATASET": EvaluationDatasetSummary,
    "EVALUATION_COMPARISON": GovernedEvaluationComparisonResult,
    "EVALUATION_RUN": GovernedEvaluationRunResult,
    "DRIFT_SIGNAL": DriftSignal,
    "AI_INCIDENT": IncidentSummary,
    "IMPROVEMENT_BACKLOG": ImprovementBacklogItem,
    "TOKEN_BUDGET": TokenBudgetSummary,
}
