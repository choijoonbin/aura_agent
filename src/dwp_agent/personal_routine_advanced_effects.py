from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from .governed_domain_core import GovernedDomainConflict
from .personal_routine_advanced_contracts import (
    RoutineAdvancedCommandKind,
    RoutineAgentEngineSwitchPayload,
    RoutineTemporaryBudgetIncreasePayload,
)
from .personal_routine_advanced_provider import (
    RoutineAdvancedProviderError,
    RoutineAdvancedProviderResult,
)


@dataclass(frozen=True)
class RoutineRuntimePolicy:
    engine_agent_id: str | None = None
    engine_id: str | None = None
    engine_state_version: int | None = None
    engine_source_command_id: UUID | None = None
    engine_expires_at: datetime | None = None
    additional_tokens_per_run: int = 0
    additional_minutes_per_run: int = 0
    budget_exception_command_ids: tuple[UUID, ...] = ()

    def provider_payload(self) -> dict[str, object]:
        engine = None
        if self.engine_state_version is not None:
            engine = {
                "agentId": self.engine_agent_id,
                "engineId": self.engine_id,
                "stateVersion": self.engine_state_version,
                "sourceCommandId": str(self.engine_source_command_id),
                "expiresAt": self.engine_expires_at.isoformat(),
            }
        return {
            "engineOverride": engine,
            "budgetExceptionCommandIds": [
                str(value) for value in self.budget_exception_command_ids
            ],
            "additionalTokensPerRun": self.additional_tokens_per_run,
            "additionalMinutesPerRun": self.additional_minutes_per_run,
        }


def apply_advanced_runtime_effect(
    connection: Any,
    row: Any,
    result: RoutineAdvancedProviderResult,
) -> None:
    if result.state != "SUCCEEDED":
        return
    payload = result.result.applied_payload
    if payload.kind == RoutineAdvancedCommandKind.AGENT_ENGINE_SWITCH:
        _apply_engine_override(connection, row, result, payload)
    elif payload.kind == RoutineAdvancedCommandKind.TEMPORARY_BUDGET_INCREASE:
        _apply_budget_exception(connection, row, result, payload)


def load_routine_runtime_policy(
    connection: Any,
    *,
    tenant_id: int,
    user_id: str,
    routine_id: UUID,
    reference: datetime,
) -> RoutineRuntimePolicy:
    engine = connection.execute(
        """SELECT command_id, state_version, action, agent_id, engine_id, expires_at
             FROM ai_personal_routine_engine_override_events
            WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
            ORDER BY state_version DESC LIMIT 1""",
        (tenant_id, user_id, routine_id),
    ).fetchone()
    active_engine = (
        engine
        if engine is not None
        and engine["action"] == "APPLY"
        and engine["expires_at"] > reference
        else None
    )
    budget = connection.execute(
        """SELECT COALESCE(SUM(additional_tokens_per_run), 0) AS tokens,
                  COALESCE(SUM(additional_minutes_per_run), 0) AS minutes,
                  COALESCE(ARRAY_AGG(command_id ORDER BY expires_at, command_id)
                      FILTER (WHERE command_id IS NOT NULL), ARRAY[]::uuid[]) AS commands
             FROM ai_personal_routine_budget_exceptions
            WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
              AND expires_at > %s""",
        (tenant_id, user_id, routine_id, reference),
    ).fetchone()
    return RoutineRuntimePolicy(
        engine_agent_id=active_engine["agent_id"] if active_engine else None,
        engine_id=active_engine["engine_id"] if active_engine else None,
        engine_state_version=(int(active_engine["state_version"]) if active_engine else None),
        engine_source_command_id=(active_engine["command_id"] if active_engine else None),
        engine_expires_at=active_engine["expires_at"] if active_engine else None,
        additional_tokens_per_run=int(budget["tokens"]),
        additional_minutes_per_run=int(budget["minutes"]),
        budget_exception_command_ids=tuple(budget["commands"]),
    )


def reserve_monthly_run_budget(
    connection: Any,
    *,
    routine_run_id: UUID,
    tenant_id: int,
    user_id: str,
    routine_id: UUID,
    maximum_runs: int,
    reference: datetime,
) -> UUID | None:
    connection.execute(
        """SELECT routine_id FROM ai_personal_routines
            WHERE routine_id = %s AND tenant_id = %s AND user_id = %s
            FOR UPDATE""",
        (routine_id, tenant_id, user_id),
    ).fetchone()
    month = reference.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    used = connection.execute(
        """SELECT COUNT(*) AS count
             FROM ai_personal_routine_executions
            WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
              AND routine_run_id <> %s AND created_at >= %s
              AND safe_error_code IS DISTINCT FROM 'ROUTINE_MONTHLY_BUDGET_EXHAUSTED'""",
        (tenant_id, user_id, routine_id, routine_run_id, month),
    ).fetchone()
    if int(used["count"]) < maximum_runs:
        return None
    exceptions = connection.execute(
        """SELECT command_id, additional_runs
             FROM ai_personal_routine_budget_exceptions
            WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
              AND expires_at > %s AND additional_runs > 0
            ORDER BY expires_at, command_id
            FOR UPDATE""",
        (tenant_id, user_id, routine_id, reference),
    ).fetchall()
    for exception in exceptions:
        consumption = connection.execute(
            """SELECT COUNT(*) AS count
                 FROM ai_personal_routine_budget_run_consumptions
                WHERE exception_command_id = %s AND budget_month = %s""",
            (exception["command_id"], month.date()),
        ).fetchone()
        if int(consumption["count"]) >= int(exception["additional_runs"]):
            continue
        connection.execute(
            """INSERT INTO ai_personal_routine_budget_run_consumptions (
                   routine_run_id, exception_command_id, routine_id,
                   tenant_id, user_id, budget_month)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                routine_run_id,
                exception["command_id"],
                routine_id,
                tenant_id,
                user_id,
                month.date(),
            ),
        )
        return exception["command_id"]
    raise GovernedDomainConflict("The routine monthly run budget is exhausted.")


def _apply_engine_override(
    connection: Any,
    row: Any,
    result: RoutineAdvancedProviderResult,
    payload: RoutineAgentEngineSwitchPayload,
) -> None:
    connection.execute(
        """SELECT routine_id FROM ai_personal_routines
            WHERE routine_id = %s AND tenant_id = %s AND user_id = %s
            FOR UPDATE""",
        (row["routine_id"], row["tenant_id"], row["user_id"]),
    ).fetchone()
    now = connection.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
    if payload.action == "APPLY" and payload.expires_at <= now:
        raise RoutineAdvancedProviderError(
            "ROUTINE_ENGINE_OVERRIDE_EXPIRED",
            "Request a new engine override window and obtain a fresh provider receipt.",
        )
    current = connection.execute(
        """SELECT COALESCE(MAX(state_version), 0) AS version
             FROM ai_personal_routine_engine_override_events
            WHERE tenant_id = %s AND user_id = %s AND routine_id = %s""",
        (row["tenant_id"], row["user_id"], row["routine_id"]),
    ).fetchone()
    connection.execute(
        """INSERT INTO ai_personal_routine_engine_override_events (
               event_id, command_id, routine_id, tenant_id, user_id,
               state_version, action, agent_id, engine_id, expires_at,
               provider_receipt_id, result_sha256, evidence_ref)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            uuid4(),
            row["command_id"],
            row["routine_id"],
            row["tenant_id"],
            row["user_id"],
            int(current["version"]) + 1,
            payload.action,
            payload.agent_id,
            payload.engine_id,
            payload.expires_at,
            result.provider_receipt_id,
            result.result_sha256,
            result.result.evidence_ref,
        ),
    )


def _apply_budget_exception(
    connection: Any,
    row: Any,
    result: RoutineAdvancedProviderResult,
    payload: RoutineTemporaryBudgetIncreasePayload,
) -> None:
    connection.execute(
        """SELECT routine_id FROM ai_personal_routines
            WHERE routine_id = %s AND tenant_id = %s AND user_id = %s
            FOR UPDATE""",
        (row["routine_id"], row["tenant_id"], row["user_id"]),
    ).fetchone()
    now = connection.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
    if payload.expires_at <= now:
        raise RoutineAdvancedProviderError(
            "ROUTINE_BUDGET_EXCEPTION_EXPIRED",
            "Request a new temporary budget window and obtain a fresh provider receipt.",
        )
    active = connection.execute(
        """SELECT COALESCE(SUM(additional_runs), 0) AS runs,
                  COALESCE(SUM(additional_tokens_per_run), 0) AS tokens,
                  COALESCE(SUM(additional_minutes_per_run), 0) AS minutes
             FROM ai_personal_routine_budget_exceptions
            WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
              AND expires_at > %s""",
        (row["tenant_id"], row["user_id"], row["routine_id"], now),
    ).fetchone()
    totals = (
        int(active["runs"]) + payload.additional_runs,
        int(active["tokens"]) + payload.additional_tokens_per_run,
        int(active["minutes"]) + payload.additional_minutes_per_run,
    )
    if totals[0] > 744 or totals[1] > 2_000_000 or totals[2] > 240:
        raise RoutineAdvancedProviderError(
            "ROUTINE_BUDGET_EXCEPTION_LIMIT_EXCEEDED",
            "Wait for an active exception to expire or request a smaller bounded increase.",
        )
    connection.execute(
        """INSERT INTO ai_personal_routine_budget_exceptions (
               command_id, routine_id, tenant_id, user_id, additional_runs,
               additional_tokens_per_run, additional_minutes_per_run, expires_at,
               provider_receipt_id, result_sha256, evidence_ref)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            row["command_id"],
            row["routine_id"],
            row["tenant_id"],
            row["user_id"],
            payload.additional_runs,
            payload.additional_tokens_per_run,
            payload.additional_minutes_per_run,
            payload.expires_at,
            result.provider_receipt_id,
            result.result_sha256,
            result.result.evidence_ref,
        ),
    )
