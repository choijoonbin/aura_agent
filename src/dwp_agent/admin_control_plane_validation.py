from __future__ import annotations

import hashlib
from typing import Any

from .canonical_json import canonical_json_bytes


def normalize_governed_review(
    reason: str,
    evidence_refs: list[str],
    ticket_ref: str | None = None,
) -> tuple[str, list[str], str | None]:
    normalized_reason = _normalized_text(reason, "reason", minimum=5, maximum=2_000)
    normalized_ticket = (
        _normalized_text(ticket_ref, "ticketRef", maximum=240)
        if ticket_ref is not None
        else None
    )
    if not evidence_refs:
        raise ValueError("evidenceRefs must contain at least one reference.")
    normalized_evidence = [
        _normalized_text(reference, "evidenceRefs item", maximum=240)
        for reference in evidence_refs
    ]
    if len(set(normalized_evidence)) != len(normalized_evidence):
        raise ValueError("evidenceRefs must be unique after whitespace normalization.")
    return normalized_reason, normalized_evidence, normalized_ticket


def normalize_command_preflight(
    changes: list[Any], impact_scopes: list[str], recovery_plan: str
) -> tuple[list[str], str]:
    for change in changes:
        change.field = _normalized_text(
            change.field, "changes.field", maximum=160
        )
    normalized_scopes = [
        _normalized_text(scope, "impactScopes item", maximum=160)
        for scope in impact_scopes
    ]
    if len(set(normalized_scopes)) != len(normalized_scopes):
        raise ValueError("impactScopes must be unique after whitespace normalization.")
    return normalized_scopes, _normalized_text(
        recovery_plan, "recoveryPlan", minimum=5, maximum=8_000
    )


def normalize_worker_completion(
    *, tenant_id: int | None, attempt_id: object | None, correlation_id: str | None,
    summary: str | None, receipt_ref: str | None,
    snapshot: dict[str, Any] | None, result_version: int | None,
    result_sha256: str | None,
) -> tuple[str, str, str]:
    if (tenant_id is None or attempt_id is None or result_version is None
            or not snapshot or not result_sha256):
        raise ValueError(
            "Successful worker observations require bound context, a domain receipt, "
            "and a non-empty versioned result snapshot digest."
        )
    normalized = (
        _normalized_text(correlation_id or "", "correlationId", maximum=160),
        _normalized_text(summary or "", "resultSummary", maximum=2_000),
        _normalized_text(receipt_ref or "", "domainReceiptRef", maximum=500),
    )
    if hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest() != result_sha256:
        raise ValueError("resultSha256 does not match the canonical resultSnapshot.")
    return normalized


def validate_special_admin_command(
    *,
    kind: str,
    target_type: str,
    target_id: str,
    expected_version: int,
    payload: dict[str, Any],
) -> None:
    if kind == "TOKEN_BUDGET_UPDATE":
        if target_type != "TOKEN_BUDGET" or target_id != "ASK_RUNTIME":
            raise ValueError("Token budget updates require the ASK_RUNTIME tenant budget.")
        budget = payload.get("budgetTokens")
        if (
            isinstance(budget, bool)
            or not isinstance(budget, int)
            or not 1 <= budget <= 10_000_000_000
        ):
            raise ValueError("budgetTokens must be a whole number between 1 and 10000000000.")
        if payload.get("policyMode") not in {"WARN", "THROTTLE", "BLOCK"}:
            raise ValueError("policyMode must be WARN, THROTTLE, or BLOCK.")
    elif kind == "DATASET_PII_DECIDE":
        _validate_pii_decision(target_type, payload)
    elif kind == "EVALUATION_COMPARE":
        _validate_comparison(target_type, target_id, payload)
    elif kind in {"EVALUATION_RUN", "EVALUATION_RERUN"}:
        _validate_evaluation_execution(
            kind, target_type, target_id, expected_version, payload
        )


def _validate_pii_decision(target_type: str, payload: dict[str, Any]) -> None:
    if target_type != "EVALUATION_DATASET":
        raise ValueError("PII decisions require an evaluation dataset target.")
    if payload.get("decision") not in {"PASS", "BLOCKED"}:
        raise ValueError("PII decisions must be PASS or BLOCKED.")
    evidence = payload.get("evidenceRefs")
    if not isinstance(evidence, list) or not evidence or not all(
        isinstance(item, str) and item.strip() for item in evidence
    ):
        raise ValueError("PII decisions require evidence references.")
    _required_text(payload, "reviewerNote", "PII decisions")


def _validate_comparison(
    target_type: str, target_id: str, payload: dict[str, Any]
) -> None:
    if target_type != "EVALUATION_DATASET":
        raise ValueError("Evaluation comparisons require an evaluation dataset target.")
    if payload.get("datasetId") != target_id:
        raise ValueError("The comparison dataset must match the governed target.")
    for key in (
        "baseline",
        "candidate",
        "promptVersion",
        "policyVersion",
        "toolVersion",
        "evaluatorVersion",
    ):
        _required_text(payload, key, "Evaluation comparisons")


def _validate_evaluation_execution(
    kind: str,
    target_type: str,
    target_id: str,
    expected_version: int,
    payload: dict[str, Any],
) -> None:
    if target_type != "EVALUATION_DATASET":
        raise ValueError("Evaluation execution requires an evaluation dataset target.")
    if payload.get("datasetId") != target_id:
        raise ValueError("The evaluation dataset must match the governed target.")
    dataset_version = payload.get("datasetVersion")
    if (
        isinstance(dataset_version, bool)
        or not isinstance(dataset_version, int)
        or dataset_version != expected_version
        or dataset_version < 1
    ):
        raise ValueError(
            "Evaluation execution requires datasetVersion to match expectedVersion."
        )
    if kind == "EVALUATION_RERUN":
        _required_text(payload, "comparisonId", "Evaluation reruns")


def _required_text(payload: dict[str, Any], key: str, label: str) -> None:
    if not isinstance(payload.get(key), str) or not str(payload[key]).strip():
        raise ValueError(f"{label} require {key}.")


def _normalized_text(
    value: str,
    label: str,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> str:
    normalized = value.strip()
    if len(normalized) < minimum:
        raise ValueError(f"{label} must contain at least {minimum} non-whitespace characters.")
    if maximum is not None and len(normalized) > maximum:
        raise ValueError(f"{label} must contain at most {maximum} characters.")
    return normalized
