from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from uuid import UUID

from .ai_control_contracts import (
    AIUsageObservation,
    BudgetEnforcementMode,
    EvaluationEvidenceState,
    EvaluationGateStatus,
    MeasurementFreshness,
    TenantAIExecutionPolicy,
)
from .ask_warning_contracts import AskRuntimeWarning


class AIControlUnavailable(RuntimeError):
    pass


class AIControlPolicyNotConfigured(RuntimeError):
    pass


class AIControlConflict(RuntimeError):
    pass


class AIControlDenied(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AIUsageReservation:
    reservation_id: UUID
    tenant_id: str
    run_id: str
    attempt_generation: int
    reserved_tokens: int
    policy_version: int


@dataclass(frozen=True)
class AIRuntimePlan:
    tenant_id: str
    provider: str
    model: str
    policy_version: int
    max_output_tokens: int
    warnings: tuple[AskRuntimeWarning, ...]


class AIControlRuntimeStore(Protocol):
    def policy(self, *, tenant_id: str) -> TenantAIExecutionPolicy: ...

    def usage(self, *, tenant_id: str, now: datetime) -> AIUsageObservation: ...

    def reserve(
        self,
        *,
        tenant_id: str,
        run_id: str,
        attempt_generation: int,
        policy_version: int,
        requested_tokens: int,
        now: datetime,
    ) -> AIUsageReservation: ...

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
    ) -> None: ...

    def release(
        self,
        *,
        tenant_id: str,
        reservation_id: UUID,
        run_id: str,
        attempt_generation: int,
        now: datetime,
    ) -> None: ...


class AIRuntimeControl:
    def __init__(
        self,
        store: AIControlRuntimeStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def preflight(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        knowledge_sources: tuple[str, ...],
        now: datetime,
        region: str | None = None,
    ) -> AIRuntimePlan:
        policy = self.store.policy(tenant_id=tenant_id)
        provider_key = provider.strip().upper()
        model_key = model.strip()
        if policy.emergency_disabled:
            raise AIControlDenied("AI_RUNTIME_EMERGENCY_DISABLED")
        runtime_region = region.strip() if region else None
        if not any(
            route.provider == provider_key
            and route.model == model_key
            and (route.region is None or route.region == runtime_region)
            for route in policy.allowed_model_routes
        ):
            raise AIControlDenied("AI_MODEL_ROUTE_NOT_ALLOWED")
        requested_sources = {source.strip().upper() for source in knowledge_sources}
        blocked_sources = requested_sources - set(policy.allowed_knowledge_sources)
        if blocked_sources:
            raise AIControlDenied("AI_KNOWLEDGE_SOURCE_NOT_ALLOWED")
        if policy.require_evaluation_pass:
            observed_at = policy.evaluation_observed_at
            evidence_is_current = (
                policy.evaluation_gate_status == EvaluationGateStatus.PASSED
                and policy.evaluation_evidence_state == EvaluationEvidenceState.VERIFIED
                and policy.evaluation_policy_version == policy.policy_version
                and observed_at is not None
                and _as_utc(now) - timedelta(days=30)
                <= _as_utc(observed_at)
                <= _as_utc(now) + timedelta(minutes=5)
            )
            if not evidence_is_current:
                raise AIControlDenied("AI_EVALUATION_GATE_NOT_PASSED")
        usage = self.store.usage(tenant_id=tenant_id, now=now)
        warnings = runtime_warnings(policy, usage)
        return AIRuntimePlan(
            tenant_id=tenant_id,
            provider=provider_key,
            model=model_key,
            policy_version=policy.policy_version,
            max_output_tokens=policy.max_output_tokens_per_request,
            warnings=warnings,
        )

    def authorize_tool(
        self,
        *,
        tenant_id: str,
        tool_key: str,
    ) -> None:
        policy = self.store.policy(tenant_id=tenant_id)
        if policy.emergency_disabled:
            raise AIControlDenied("AI_RUNTIME_EMERGENCY_DISABLED")
        if tool_key.strip().upper() not in set(policy.allowed_tool_keys):
            raise AIControlDenied("AI_TOOL_NOT_ALLOWED")

    def reserve(
        self,
        *,
        plan: AIRuntimePlan,
        run_id: str,
        attempt_generation: int,
        input_token_ceiling: int,
        now: datetime,
    ) -> AIUsageReservation:
        if attempt_generation < 1 or input_token_ceiling < 0:
            raise ValueError("AI usage reservation identity and ceiling must be valid.")
        return self.store.reserve(
            tenant_id=plan.tenant_id,
            run_id=run_id,
            attempt_generation=attempt_generation,
            policy_version=plan.policy_version,
            requested_tokens=input_token_ceiling + plan.max_output_tokens,
            now=now,
        )

    def settle(
        self,
        *,
        reservation: AIUsageReservation,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        now: datetime,
    ) -> None:
        self.store.settle(
            tenant_id=reservation.tenant_id,
            reservation_id=reservation.reservation_id,
            run_id=reservation.run_id,
            attempt_generation=reservation.attempt_generation,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            usage_observed=True,
            now=now,
        )

    def release(self, *, reservation: AIUsageReservation, now: datetime) -> None:
        self.store.release(
            tenant_id=reservation.tenant_id,
            reservation_id=reservation.reservation_id,
            run_id=reservation.run_id,
            attempt_generation=reservation.attempt_generation,
            now=now,
        )

    def invoke_model(
        self,
        *,
        plan: AIRuntimePlan,
        run_id: str,
        attempt_generation: int,
        input_token_ceiling: int,
        call: Callable[[int], Any],
        now: datetime,
    ) -> Any:
        reservation = self.reserve(
            plan=plan,
            run_id=run_id,
            attempt_generation=attempt_generation,
            input_token_ceiling=input_token_ceiling,
            now=now,
        )
        try:
            result = call(plan.max_output_tokens)
        except Exception as error:
            completed_at = self.clock()
            if bool(getattr(error, "usage_observed", False)):
                self.store.settle(
                    tenant_id=reservation.tenant_id,
                    reservation_id=reservation.reservation_id,
                    run_id=reservation.run_id,
                    attempt_generation=reservation.attempt_generation,
                    provider=str(getattr(error, "provider", plan.provider)),
                    model=str(getattr(error, "model", plan.model)),
                    input_tokens=int(getattr(error, "input_tokens")),
                    output_tokens=int(getattr(error, "output_tokens")),
                    total_tokens=int(getattr(error, "total_tokens")),
                    usage_observed=True,
                    now=completed_at,
                )
            elif bool(getattr(error, "provider_attempted", True)):
                self.store.settle(
                    tenant_id=reservation.tenant_id,
                    reservation_id=reservation.reservation_id,
                    run_id=reservation.run_id,
                    attempt_generation=reservation.attempt_generation,
                    provider=plan.provider,
                    model=plan.model,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    usage_observed=False,
                    now=completed_at,
                )
            else:
                try:
                    self.release(reservation=reservation, now=completed_at)
                except (AIControlConflict, AIControlUnavailable):
                    pass
            raise
        completed_at = self.clock()
        if bool(getattr(result, "usage_observed", True)):
            self.settle(
                reservation=reservation,
                provider=str(result.provider),
                model=str(result.model),
                input_tokens=int(result.input_tokens),
                output_tokens=int(result.output_tokens),
                total_tokens=int(result.total_tokens),
                now=completed_at,
            )
        else:
            self.store.settle(
                tenant_id=reservation.tenant_id,
                reservation_id=reservation.reservation_id,
                run_id=reservation.run_id,
                attempt_generation=reservation.attempt_generation,
                provider=str(result.provider),
                model=str(result.model),
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                usage_observed=False,
                now=completed_at,
            )
        return result


def budget_warnings(
    policy: TenantAIExecutionPolicy,
    usage: AIUsageObservation,
) -> tuple[AskRuntimeWarning, ...]:
    limit = policy.period_token_limit
    if limit is None:
        return ()
    projected = usage.measured_total_tokens + usage.reserved_tokens
    threshold = (limit * policy.alert_threshold_percent) // 100
    warnings: list[AskRuntimeWarning] = []
    if projected >= threshold:
        warnings.append(AskRuntimeWarning.AI_TOKEN_BUDGET_ALERT_THRESHOLD_REACHED)
    if projected >= limit and policy.budget_enforcement_mode == BudgetEnforcementMode.ALERT_ONLY:
        warnings.append(AskRuntimeWarning.AI_TOKEN_BUDGET_EXCEEDED_ALERT_ONLY)
    return tuple(warnings)


def runtime_warnings(
    policy: TenantAIExecutionPolicy,
    usage: AIUsageObservation,
) -> tuple[AskRuntimeWarning, ...]:
    warnings: list[AskRuntimeWarning] = []
    if usage.unmeasured_reserved_tokens:
        warnings.append(AskRuntimeWarning.MODEL_USAGE_MEASUREMENT_MISSING)
    if usage.measurement_freshness != MeasurementFreshness.CURRENT:
        warnings.append(AskRuntimeWarning.LOCAL_USAGE_MEASUREMENT_NOT_CURRENT)
    warnings.extend(budget_warnings(policy, usage))
    return tuple(warnings)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("AI control timestamps must include a timezone.")
    return value.astimezone(timezone.utc)
