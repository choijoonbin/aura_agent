from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from psycopg import connect

from .ai_control_activation import ai_runtime_control_activation_state
from .ai_control_audit import record_ai_control_event
from .ai_control_contracts import (
    AIControlOverview,
    AIUsageObservation,
    BootstrapAIExecutionPolicyRequest,
    BudgetEnforcementMode,
    EvaluationEvidenceState,
    ExternalDataState,
    MeasurementFreshness,
    ModelRoutePolicy,
    RuntimeControlState,
    SetAIEmergencyDisableRequest,
    TenantAIExecutionPolicy,
    ToolEnforcementState,
    UpdateAIExecutionPolicyRequest,
)
from .ai_control_runtime import (
    AIControlConflict,
    AIControlDenied,
    AIControlPolicyNotConfigured,
    AIControlUnavailable,
    AIUsageReservation,
    runtime_warnings,
)
from .ai_control_store_queries import (
    insert_policy_sql as _insert_policy_sql,
    period_bounds as _period_bounds,
    request_fingerprint as _request_fingerprint,
    request_values as _request_values,
    update_policy_sql as _update_policy_sql,
)


class PostgresAIControlStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def policy(self, *, tenant_id: str) -> TenantAIExecutionPolicy:
        try:
            with connect(self.database_url) as connection:
                return self._policy(connection, int(tenant_id))
        except (AIControlPolicyNotConfigured, ValueError):
            raise
        except Exception as error:
            raise AIControlUnavailable("AI runtime policy storage is unavailable.") from error

    def usage(self, *, tenant_id: str, now: datetime) -> AIUsageObservation:
        try:
            with connect(self.database_url) as connection:
                return self._usage(connection, int(tenant_id), now)
        except ValueError:
            raise
        except Exception as error:
            raise AIControlUnavailable("AI usage measurement storage is unavailable.") from error

    def overview(self, *, tenant_id: str, now: datetime) -> AIControlOverview:
        try:
            with connect(self.database_url) as connection:
                usage = self._usage(connection, int(tenant_id), now)
                try:
                    policy = self._policy(connection, int(tenant_id))
                except AIControlPolicyNotConfigured:
                    return AIControlOverview(
                        enforcement_activation_state=ai_runtime_control_activation_state(),
                        runtime_control_state=RuntimeControlState.POLICY_NOT_CONFIGURED,
                        tool_enforcement_state=ToolEnforcementState.NOT_CONNECTED,
                        policy=None,
                        usage=usage,
                        warnings=["AI_RUNTIME_POLICY_NOT_CONFIGURED"],
                    )
                state = (
                    RuntimeControlState.EMERGENCY_DISABLED
                    if policy.emergency_disabled
                    else RuntimeControlState.ENABLED
                )
                warnings = [
                    "PROVIDER_USAGE_UNAVAILABLE",
                    "PROVIDER_PRICING_UNAVAILABLE",
                    "PROVIDER_BILLING_UNAVAILABLE",
                    "AI_TOOL_EXECUTION_NOT_CONNECTED",
                ]
                warnings.extend(runtime_warnings(policy, usage))
                return AIControlOverview(
                    enforcement_activation_state=ai_runtime_control_activation_state(),
                    runtime_control_state=state,
                    tool_enforcement_state=ToolEnforcementState.NOT_CONNECTED,
                    policy=policy,
                    usage=usage,
                    warnings=warnings,
                )
        except ValueError:
            raise
        except Exception as error:
            raise AIControlUnavailable("AI runtime control is unavailable.") from error

    def reserve(
        self,
        *,
        tenant_id: str,
        run_id: str,
        attempt_generation: int,
        policy_version: int,
        requested_tokens: int,
        now: datetime,
    ) -> AIUsageReservation:
        tenant = int(tenant_id)
        run_uuid = UUID(run_id)
        if attempt_generation < 1:
            raise ValueError("AI usage attempt generation must be positive.")
        try:
            with connect(self.database_url) as connection:
                policy = self._policy(connection, tenant, lock=True)
                existing = connection.execute(
                    """SELECT state
                         FROM ai_runtime_usage_reservations
                        WHERE tenant_id = %s AND run_id = %s
                          AND attempt_generation = %s""",
                    (tenant, run_uuid, attempt_generation),
                ).fetchone()
                if existing is not None:
                    raise AIControlConflict(
                        "This AI run generation was already admitted and cannot invoke "
                        "the provider again."
                    )
                if policy.policy_version != policy_version:
                    raise AIControlConflict("AI runtime policy changed. Retry the request.")
                if policy.emergency_disabled:
                    raise AIControlDenied("AI_RUNTIME_EMERGENCY_DISABLED")
                usage = self._usage(connection, tenant, now)
                projected = (
                    usage.measured_total_tokens + usage.reserved_tokens + requested_tokens
                )
                if (
                    policy.budget_enforcement_mode == BudgetEnforcementMode.ENFORCED
                    and policy.period_token_limit is not None
                    and projected > policy.period_token_limit
                ):
                    raise AIControlDenied("AI_TOKEN_BUDGET_HARD_LIMIT")
                reservation_id = uuid4()
                period_start, _ = _period_bounds(now)
                connection.execute(
                    """INSERT INTO ai_runtime_usage_reservations (
                           reservation_id, tenant_id, run_id, attempt_generation, period_start,
                           reserved_tokens, policy_version, expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        reservation_id, tenant, run_uuid, attempt_generation, period_start,
                        requested_tokens,
                        policy_version, now + timedelta(minutes=10),
                    ),
                )
                return AIUsageReservation(
                    reservation_id=reservation_id, tenant_id=tenant_id, run_id=run_id,
                    attempt_generation=attempt_generation,
                    reserved_tokens=requested_tokens, policy_version=policy_version,
                )
        except (AIControlConflict, AIControlDenied, AIControlPolicyNotConfigured, ValueError):
            raise
        except Exception as error:
            raise AIControlUnavailable("AI token reservation is unavailable.") from error

    def settle(
        self,
        *,
        tenant_id: str,
        reservation_id: UUID,
        run_id: str,
        attempt_generation: int,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        usage_observed: bool,
        now: datetime,
    ) -> None:
        tenant = int(tenant_id)
        run_uuid = UUID(run_id)
        measured_total = max(total_tokens, input_tokens + output_tokens)
        try:
            with connect(self.database_url) as connection:
                row = connection.execute(
                    """SELECT state, period_start
                         FROM ai_runtime_usage_reservations
                        WHERE tenant_id = %s AND reservation_id = %s AND run_id = %s
                          AND attempt_generation = %s
                        FOR UPDATE""",
                    (tenant, reservation_id, run_uuid, attempt_generation),
                ).fetchone()
                if row is None:
                    raise AIControlConflict("AI usage reservation was not found for this tenant.")
                policy = connection.execute(
                    "SELECT 1 FROM ai_execution_policies WHERE tenant_id = %s FOR UPDATE", (tenant,)
                ).fetchone()
                if policy is None:
                    raise AIControlConflict("Tenant AI runtime policy is not configured.")
                if row[0] == "SETTLED":
                    return
                if row[0] == "MEASUREMENT_MISSING":
                    return
                if row[0] != "ACTIVE":
                    raise AIControlConflict("AI usage reservation is no longer active.")
                if not usage_observed:
                    connection.execute(
                        """UPDATE ai_runtime_usage_reservations
                              SET state = 'MEASUREMENT_MISSING', completed_at = %s
                            WHERE tenant_id = %s AND reservation_id = %s AND run_id = %s
                              AND attempt_generation = %s""",
                        (now, tenant, reservation_id, run_uuid, attempt_generation),
                    )
                    return
                measurement = connection.execute(
                    """INSERT INTO ai_runtime_usage_measurements (
                           measurement_id, tenant_id, reservation_id, run_id,
                           attempt_generation, provider, model, input_tokens,
                           output_tokens, total_tokens, observed_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (tenant_id, run_id, attempt_generation) DO NOTHING
                       RETURNING measurement_id""",
                    (
                        uuid4(), tenant, reservation_id, run_uuid, attempt_generation,
                        provider[:40], model[:160], input_tokens, output_tokens,
                        measured_total, now,
                    ),
                ).fetchone()
                if measurement is not None:
                    _, period_end = _period_bounds(row[1])
                    connection.execute(
                        """INSERT INTO ai_runtime_usage_periods (
                               tenant_id, period_start, period_end, measured_input_tokens,
                               measured_output_tokens, measured_total_tokens,
                               measurement_observed_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (tenant_id, period_start) DO UPDATE SET
                               measured_input_tokens = ai_runtime_usage_periods.measured_input_tokens
                                   + EXCLUDED.measured_input_tokens,
                               measured_output_tokens = ai_runtime_usage_periods.measured_output_tokens
                                   + EXCLUDED.measured_output_tokens,
                               measured_total_tokens = ai_runtime_usage_periods.measured_total_tokens
                                   + EXCLUDED.measured_total_tokens,
                               measurement_observed_at = GREATEST(
                                   ai_runtime_usage_periods.measurement_observed_at,
                                   EXCLUDED.measurement_observed_at)""",
                        (
                            tenant, row[1], period_end, input_tokens, output_tokens,
                            measured_total, now,
                        ),
                    )
                connection.execute(
                    """UPDATE ai_runtime_usage_reservations
                          SET state = 'SETTLED', completed_at = %s
                        WHERE tenant_id = %s AND reservation_id = %s AND run_id = %s
                          AND attempt_generation = %s""",
                    (now, tenant, reservation_id, run_uuid, attempt_generation),
                )
        except (AIControlConflict, ValueError):
            raise
        except Exception as error:
            raise AIControlUnavailable("AI usage settlement is unavailable.") from error

    def release(
        self, *, tenant_id: str, reservation_id: UUID, run_id: str,
        attempt_generation: int, now: datetime
    ) -> None:
        try:
            with connect(self.database_url) as connection:
                result = connection.execute(
                    """UPDATE ai_runtime_usage_reservations
                          SET state = 'RELEASED', completed_at = %s
                        WHERE tenant_id = %s AND reservation_id = %s AND run_id = %s
                          AND attempt_generation = %s
                          AND state = 'ACTIVE'""",
                    (now, int(tenant_id), reservation_id, UUID(run_id), attempt_generation),
                )
                if result.rowcount == 0:
                    row = connection.execute(
                        """SELECT state FROM ai_runtime_usage_reservations
                            WHERE tenant_id = %s AND reservation_id = %s AND run_id = %s
                              AND attempt_generation = %s""",
                        (int(tenant_id), reservation_id, UUID(run_id), attempt_generation),
                    ).fetchone()
                    if row is None:
                        raise AIControlConflict(
                            "AI usage reservation was not found for this tenant."
                        )
        except (AIControlConflict, ValueError):
            raise
        except Exception as error:
            raise AIControlUnavailable("AI usage reservation release is unavailable.") from error

    def bootstrap(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: BootstrapAIExecutionPolicyRequest,
    ) -> TenantAIExecutionPolicy:
        tenant = int(tenant_id)
        try:
            with connect(self.database_url) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"dwp-agent:ai-control:{tenant}",),
                )
                command_key = f"BOOTSTRAP:{request.idempotency_key}"
                fingerprint = _request_fingerprint(request)
                replayed = connection.execute(
                    """SELECT current_value FROM ai_governance_events
                        WHERE tenant_id = %s AND category = 'AI_CONTROL'
                          AND event_type = 'ai-control.policy-bootstrapped'
                          AND target_type = 'AI_EXECUTION_POLICY' AND target_key = %s""",
                    (tenant, command_key),
                ).fetchone()
                if replayed is not None:
                    if replayed[0].get("requestFingerprint") != fingerprint:
                        raise AIControlConflict(
                            "The idempotency key was already used for another AI policy."
                        )
                    return TenantAIExecutionPolicy.model_validate(replayed[0]["policy"])
                count = connection.execute(
                    "SELECT COUNT(*) FROM ai_execution_policies WHERE tenant_id = %s",
                    (tenant,),
                ).fetchone()[0]
                if count != request.expected_existing_count:
                    raise AIControlConflict("AI runtime policy already exists. Reload and retry.")
                if count == 0:
                    values = _request_values(request)
                    connection.execute(
                        _insert_policy_sql(),
                        (tenant, *values, actor_user_id),
                    )
                    current = self._policy(connection, tenant)
                    record_ai_control_event(
                        connection, tenant, "ai-control.policy-bootstrapped",
                        actor_user_id, correlation_id, request.change_reason, None, current,
                        target_key=command_key, request_fingerprint=fingerprint,
                    )
                return self._policy(connection, tenant)
        except (AIControlConflict, ValueError):
            raise
        except Exception as error:
            raise AIControlUnavailable("AI runtime policy bootstrap is unavailable.") from error

    def update(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: UpdateAIExecutionPolicyRequest,
    ) -> TenantAIExecutionPolicy:
        tenant = int(tenant_id)
        try:
            with connect(self.database_url) as connection:
                previous = self._policy(connection, tenant, lock=True)
                if previous.policy_version != request.expected_version:
                    raise AIControlConflict("AI runtime policy changed. Reload and retry.")
                result = connection.execute(
                    _update_policy_sql(),
                    (*_request_values(request), actor_user_id, tenant, request.expected_version),
                )
                if result.rowcount != 1:
                    raise AIControlConflict("AI runtime policy changed. Reload and retry.")
                current = self._policy(connection, tenant)
                record_ai_control_event(
                    connection, tenant, "ai-control.policy-updated", actor_user_id,
                    correlation_id, request.change_reason, previous, current,
                )
                return current
        except (AIControlConflict, AIControlPolicyNotConfigured, ValueError):
            raise
        except Exception as error:
            raise AIControlUnavailable("AI runtime policy update is unavailable.") from error

    def set_emergency_disabled(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: SetAIEmergencyDisableRequest,
    ) -> TenantAIExecutionPolicy:
        tenant = int(tenant_id)
        try:
            with connect(self.database_url) as connection:
                previous = self._policy(connection, tenant, lock=True)
                if previous.policy_version != request.expected_version:
                    raise AIControlConflict("AI runtime policy changed. Reload and retry.")
                connection.execute(
                    """UPDATE ai_execution_policies
                          SET emergency_disabled = %s, policy_version = policy_version + 1,
                              updated_by = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND policy_version = %s""",
                    (request.disabled, actor_user_id, tenant, request.expected_version),
                )
                current = self._policy(connection, tenant)
                record_ai_control_event(
                    connection, tenant, "ai-control.emergency-disable-changed",
                    actor_user_id, correlation_id, request.change_reason, previous, current,
                )
                return current
        except (AIControlConflict, AIControlPolicyNotConfigured, ValueError):
            raise
        except Exception as error:
            raise AIControlUnavailable("AI emergency control is unavailable.") from error

    def _policy(self, connection, tenant: int, *, lock: bool = False):
        row = connection.execute(
            """SELECT emergency_disabled, allowed_model_routes, allowed_tool_keys,
                      allowed_knowledge_sources, max_output_tokens_per_request,
                      budget_enforcement_mode, period_token_limit, alert_threshold_percent,
                      require_evaluation_pass, evaluation_gate_status,
                      evaluation_observed_at, evaluation_policy_version,
                      policy_version, updated_at
                 FROM ai_execution_policies WHERE tenant_id = %s"""
            + (" FOR UPDATE" if lock else ""),
            (tenant,),
        ).fetchone()
        if row is None:
            raise AIControlPolicyNotConfigured("Tenant AI runtime policy is not configured.")
        return TenantAIExecutionPolicy(
            emergency_disabled=row[0],
            allowed_model_routes=[ModelRoutePolicy.model_validate(item) for item in row[1]],
            allowed_tool_keys=row[2], allowed_knowledge_sources=row[3],
            max_output_tokens_per_request=row[4], budget_enforcement_mode=row[5],
            period_token_limit=row[6], alert_threshold_percent=row[7],
            require_evaluation_pass=row[8], evaluation_gate_status=row[9],
            evaluation_evidence_state=EvaluationEvidenceState.UNAVAILABLE,
            evaluation_observed_at=row[10], evaluation_policy_version=row[11],
            policy_version=row[12], updated_at=row[13],
        )

    def _usage(self, connection, tenant: int, now: datetime) -> AIUsageObservation:
        period_start, period_end = _period_bounds(now)
        row = connection.execute(
            """SELECT COALESCE(period.measured_input_tokens, 0),
                      COALESCE(period.measured_output_tokens, 0),
                      COALESCE(period.measured_total_tokens, 0),
                      period.measurement_observed_at,
                      reservations.active_reserved,
                      reservations.unmeasured_reserved
                 FROM (
                       SELECT
                       COALESCE(SUM(reserved_tokens) FILTER (
                           WHERE state = 'ACTIVE' AND expires_at > %s), 0),
                       COALESCE(SUM(reserved_tokens) FILTER (
                           WHERE state = 'MEASUREMENT_MISSING'
                              OR (state = 'ACTIVE' AND expires_at <= %s)), 0)
                 FROM ai_runtime_usage_reservations
                WHERE tenant_id = %s AND period_start = %s
                  AND state IN ('ACTIVE', 'MEASUREMENT_MISSING')
                      ) AS reservations(active_reserved, unmeasured_reserved)
            LEFT JOIN ai_runtime_usage_periods AS period
                   ON period.tenant_id = %s AND period.period_start = %s""",
            (now, now, tenant, period_start, tenant, period_start),
        ).fetchone()
        active_reserved, unmeasured_reserved = row[4], row[5]
        observed_at = row[3]
        if unmeasured_reserved or observed_at is None:
            freshness = MeasurementFreshness.UNAVAILABLE
        elif observed_at >= now - timedelta(minutes=15):
            freshness = MeasurementFreshness.CURRENT
        else:
            freshness = MeasurementFreshness.STALE
        return AIUsageObservation(
            period_start=period_start, period_end=period_end,
            measured_input_tokens=row[0],
            measured_output_tokens=row[1],
            measured_total_tokens=row[2],
            reserved_tokens=active_reserved + unmeasured_reserved,
            unmeasured_reserved_tokens=unmeasured_reserved,
            measurement_freshness=freshness,
            measurement_observed_at=observed_at,
            provider_usage_state=ExternalDataState.UNAVAILABLE,
            provider_pricing_state=ExternalDataState.UNAVAILABLE,
            provider_billing_state=ExternalDataState.UNAVAILABLE,
        )
_STORE: PostgresAIControlStore | None = None
_STORE_LOCK = threading.Lock()


def get_ai_control_store() -> PostgresAIControlStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise AIControlUnavailable("AI runtime control requires the Agent database.")
        _STORE = PostgresAIControlStore(database_url)
        return _STORE


def reset_ai_control_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
