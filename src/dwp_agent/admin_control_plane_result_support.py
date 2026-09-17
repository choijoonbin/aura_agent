from __future__ import annotations

from .admin_control_plane_contracts import GovernedCommandKind
from .admin_control_plane_registry import (
    AdminCommandResourceStrategy,
    AdminCommandSpec,
)
from .admin_external_result_contracts import GOVERNED_EXTERNAL_RESULTS


TYPED_RESULT_RESOURCE_TYPES = frozenset({
    "MODEL_ROUTE_SIMULATION",
    "CONNECTOR",
    "EVALUATION_DATASET",
    "EVALUATION_COMPARISON",
    "EVALUATION_RUN",
    "DRIFT_SIGNAL",
    "AI_INCIDENT",
    "IMPROVEMENT_BACKLOG",
    "TOKEN_BUDGET",
})


def expected_dynamic_result_type(
    kind: GovernedCommandKind, spec: AdminCommandSpec
) -> str | None:
    if spec.family == "A03" and spec.service == "connector":
        return "CONNECTOR"
    if kind in {
        GovernedCommandKind.DATASET_IMPORT,
        GovernedCommandKind.EVALUATION_GATE_APPROVE,
    }:
        return "EVALUATION_DATASET"
    if (
        spec.family == "A05"
        and spec.resource_strategy == AdminCommandResourceStrategy.TARGET
        and spec.result_resource_type is None
    ):
        return "AI_INCIDENT"
    return None


def external_result_contract_available(
    kind: GovernedCommandKind, spec: AdminCommandSpec
) -> bool:
    if (
        spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY
        and spec.result_resource_type is None
    ):
        return kind in GOVERNED_EXTERNAL_RESULTS
    if spec.result_resource_type is not None:
        return (
            spec.result_resource_type in TYPED_RESULT_RESOURCE_TYPES
            or kind in GOVERNED_EXTERNAL_RESULTS
        )
    expected = expected_dynamic_result_type(kind, spec)
    return (
        expected in TYPED_RESULT_RESOURCE_TYPES
        or kind in GOVERNED_EXTERNAL_RESULTS
    )
