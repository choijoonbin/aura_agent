from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone


def request_values(request):
    routes = [route.model_dump(mode="json") for route in request.allowed_model_routes]
    return (
        json.dumps(routes),
        json.dumps(request.allowed_tool_keys),
        json.dumps(request.allowed_knowledge_sources),
        request.max_output_tokens_per_request,
        request.budget_enforcement_mode.value,
        request.period_token_limit,
        request.alert_threshold_percent,
        request.require_evaluation_pass,
        request.evaluation_gate_status.value,
        request.evaluation_observed_at,
        request.evaluation_policy_version,
    )


def request_fingerprint(request) -> str:
    payload = request.model_dump(mode="json", by_alias=True, exclude={"idempotency_key"})
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def insert_policy_sql() -> str:
    return """INSERT INTO ai_execution_policies (
        tenant_id, allowed_model_routes, allowed_tool_keys, allowed_knowledge_sources,
        max_output_tokens_per_request, budget_enforcement_mode, period_token_limit,
        alert_threshold_percent, require_evaluation_pass, evaluation_gate_status,
        evaluation_observed_at, evaluation_policy_version, updated_by)
        VALUES (%s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""


def update_policy_sql() -> str:
    return """UPDATE ai_execution_policies SET
        allowed_model_routes = %s::jsonb, allowed_tool_keys = %s::jsonb,
        allowed_knowledge_sources = %s::jsonb, max_output_tokens_per_request = %s,
        budget_enforcement_mode = %s, period_token_limit = %s,
        alert_threshold_percent = %s, require_evaluation_pass = %s,
        evaluation_gate_status = %s, evaluation_observed_at = %s,
        evaluation_policy_version = %s, policy_version = policy_version + 1,
        updated_by = %s, updated_at = CURRENT_TIMESTAMP
        WHERE tenant_id = %s AND policy_version = %s"""


def period_bounds(value: datetime) -> tuple[datetime, datetime]:
    current = value.astimezone(timezone.utc)
    start = datetime(current.year, current.month, 1, tzinfo=timezone.utc)
    if current.month == 12:
        end = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
    return start, end
