from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from psycopg import connect

from .operations_contracts import (
    BootstrapRetentionPolicyRequest,
    DwaionOperationsOverview,
    RetentionPolicy,
    UpdateRetentionPolicyRequest,
)


class OperationsStoreUnavailable(RuntimeError):
    pass


class RetentionPolicyConflict(RuntimeError):
    pass


class RetentionPolicyNotConfigured(RuntimeError):
    pass


class OperationsStore(Protocol):
    def overview(self, *, tenant_id: str, period_days: int = 30) -> DwaionOperationsOverview: ...

    def retention_policy(self, *, tenant_id: str) -> RetentionPolicy: ...

    def bootstrap_retention_policy(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: BootstrapRetentionPolicyRequest,
    ) -> RetentionPolicy: ...

    def update_retention_policy(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: UpdateRetentionPolicyRequest,
    ) -> RetentionPolicy: ...


class PostgresOperationsStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def overview(
        self, *, tenant_id: str, period_days: int = 30
    ) -> DwaionOperationsOverview:
        tenant = int(tenant_id)
        bounded_period = max(1, min(period_days, 90))
        with connect(self.database_url) as connection:
            run = connection.execute(
                """
                SELECT COUNT(*)::BIGINT,
                       COUNT(*) FILTER (WHERE run_state = 'COMPLETED')::BIGINT,
                       COUNT(*) FILTER (WHERE run_state = 'FAILED')::BIGINT,
                       COUNT(*) FILTER (WHERE policy_outcome = 'ALLOW')::BIGINT,
                       COUNT(*) FILTER (WHERE policy_outcome = 'HANDOFF')::BIGINT,
                       COUNT(*) FILTER (WHERE policy_outcome = 'DENY')::BIGINT,
                       COUNT(*) FILTER (WHERE answer_state = 'COMPLETED')::BIGINT,
                       COUNT(*) FILTER (WHERE answer_state = 'ABSTAINED')::BIGINT,
                       COUNT(*) FILTER (
                           WHERE answer_state = 'CONFIGURATION_REQUIRED')::BIGINT,
                       COALESCE(ROUND(AVG(latency_ms)), 0)::BIGINT,
                       COALESCE(SUM(total_tokens), 0)::BIGINT,
                       COUNT(DISTINCT user_id)::BIGINT
                  FROM ai_agent_runs
                 WHERE tenant_id = %s
                   AND created_at >= CURRENT_TIMESTAMP - make_interval(days => %s)
                """,
                (tenant, bounded_period),
            ).fetchone()
            conversation = connection.execute(
                """
                SELECT COUNT(*)::BIGINT
                  FROM ai_conversations conversation
                  LEFT JOIN ai_conversation_retention_policies policy
                    ON policy.tenant_id = conversation.tenant_id
                 WHERE conversation.tenant_id = %s
                   AND (conversation.retention_until > CURRENT_TIMESTAMP
                        OR COALESCE(policy.legal_hold, FALSE))
                """,
                (tenant,),
            ).fetchone()
            feedback = connection.execute(
                """
                SELECT COUNT(*) FILTER (WHERE rating = 'UP')::BIGINT,
                       COUNT(*) FILTER (WHERE rating = 'DOWN')::BIGINT
                  FROM ai_answer_feedback
                 WHERE tenant_id = %s
                   AND created_at >= CURRENT_TIMESTAMP - make_interval(days => %s)
                """,
                (tenant, bounded_period),
            ).fetchone()
            policy = self._retention_policy(connection, tenant)

        return DwaionOperationsOverview(
            period_days=bounded_period,
            run_count=run[0],
            completed_run_count=run[1],
            failed_run_count=run[2],
            allowed_run_count=run[3],
            handed_off_run_count=run[4],
            denied_run_count=run[5],
            grounded_answer_count=run[6],
            abstained_answer_count=run[7],
            configuration_required_count=run[8],
            average_latency_ms=run[9],
            total_tokens=run[10],
            active_user_count=run[11],
            conversation_count=conversation[0],
            feedback_up_count=feedback[0],
            feedback_down_count=feedback[1],
            retention=policy,
            generated_at=datetime.now(timezone.utc),
        )

    def retention_policy(self, *, tenant_id: str) -> RetentionPolicy:
        with connect(self.database_url) as connection:
            return self._retention_policy(connection, int(tenant_id))

    def bootstrap_retention_policy(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: BootstrapRetentionPolicyRequest,
    ) -> RetentionPolicy:
        tenant = int(tenant_id)
        command_key = f"{tenant}:BOOTSTRAP:{request.idempotency_key}"
        with connect(self.database_url) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"dwp-agent:retention:{tenant}",),
            )
            replayed = connection.execute(
                """SELECT current_value FROM ai_governance_events
                    WHERE tenant_id = %s AND category = 'RETENTION'
                      AND event_type = 'retention-policy.bootstrapped'
                      AND target_type = 'RETENTION_POLICY' AND target_key = %s""",
                (tenant, command_key),
            ).fetchone()
            if replayed is not None:
                recorded = replayed[0]
                if (
                    recorded.get("retentionDays") != request.retention_days
                    or recorded.get("legalHold") != request.legal_hold
                ):
                    raise RetentionPolicyConflict(
                        "The idempotency key was already used for another retention policy."
                    )
            else:
                existing_count = connection.execute(
                    """SELECT COUNT(*) FROM ai_conversation_retention_policies
                        WHERE tenant_id = %s""",
                    (tenant,),
                ).fetchone()[0]
                if existing_count != request.expected_existing_count:
                    raise RetentionPolicyConflict(
                        "The retention policy already exists. Reload and retry."
                    )
                connection.execute(
                    """INSERT INTO ai_conversation_retention_policies (
                           tenant_id, retention_days, legal_hold)
                       VALUES (%s, %s, %s)""",
                    (tenant, request.retention_days, request.legal_hold),
                )
                connection.execute(
                    """INSERT INTO ai_governance_events (
                           event_id, tenant_id, category, event_type, target_type,
                           target_key, actor_user_id, correlation_id, change_reason,
                           previous_value, current_value)
                       VALUES (%s, %s, 'RETENTION', 'retention-policy.bootstrapped',
                               'RETENTION_POLICY', %s, %s, %s, %s,
                               %s::jsonb, %s::jsonb)""",
                    (
                        uuid4(),
                        tenant,
                        command_key,
                        actor_user_id,
                        correlation_id,
                        request.change_reason,
                        json.dumps({"existingCount": existing_count}),
                        json.dumps(
                            {
                                "retentionDays": request.retention_days,
                                "legalHold": request.legal_hold,
                                "policyVersion": 1,
                            }
                        ),
                    ),
                )
            return self._retention_policy(connection, tenant)

    def update_retention_policy(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: UpdateRetentionPolicyRequest,
    ) -> RetentionPolicy:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            current = self._retention_policy(connection, tenant, lock=True)
            if current.policy_version != request.expected_version:
                raise RetentionPolicyConflict(
                    "The retention policy changed. Reload it before saving."
                )
            retention_days = request.retention_days or current.retention_days
            legal_hold = (
                current.legal_hold if request.legal_hold is None else request.legal_hold
            )
            if (
                retention_days == current.retention_days
                and legal_hold == current.legal_hold
            ):
                return current
            next_version = current.policy_version + 1
            row = connection.execute(
                """
                UPDATE ai_conversation_retention_policies
                   SET retention_days = %s,
                       legal_hold = %s,
                       policy_version = %s,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE tenant_id = %s AND policy_version = %s
                RETURNING retention_days, legal_hold, policy_version, updated_at
                """,
                (
                    retention_days,
                    legal_hold,
                    next_version,
                    tenant,
                    request.expected_version,
                ),
            ).fetchone()
            if row is None:
                raise RetentionPolicyConflict(
                    "The retention policy changed. Reload it before saving."
                )
            connection.execute(
                """
                INSERT INTO ai_retention_policy_events (
                    event_id, tenant_id, actor_user_id, correlation_id,
                    previous_retention_days, retention_days,
                    previous_legal_hold, legal_hold,
                    previous_policy_version, policy_version, change_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    tenant,
                    actor_user_id,
                    correlation_id,
                    current.retention_days,
                    retention_days,
                    current.legal_hold,
                    legal_hold,
                    current.policy_version,
                    next_version,
                    request.change_reason,
                ),
            )
            connection.execute(
                """
                INSERT INTO ai_governance_events (
                    event_id, tenant_id, category, event_type, target_type,
                    target_key, actor_user_id, correlation_id, change_reason,
                    previous_value, current_value)
                VALUES (
                    %s, %s, 'RETENTION', 'retention-policy.updated',
                    'RETENTION_POLICY', %s, %s, %s, %s,
                    jsonb_build_object(
                        'retentionDays', %s, 'legalHold', %s,
                        'policyVersion', %s),
                    jsonb_build_object(
                        'retentionDays', %s, 'legalHold', %s,
                        'policyVersion', %s))
                """,
                (
                    uuid4(), tenant, str(tenant), actor_user_id, correlation_id,
                    request.change_reason, current.retention_days, current.legal_hold,
                    current.policy_version, retention_days, legal_hold, next_version,
                ),
            )
            return RetentionPolicy(
                retention_days=row[0],
                legal_hold=row[1],
                policy_version=row[2],
                updated_at=row[3],
            )

    def _retention_policy(
        self, connection, tenant_id: int, *, lock: bool = False
    ) -> RetentionPolicy:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT retention_days, legal_hold, policy_version, updated_at
              FROM ai_conversation_retention_policies
             WHERE tenant_id = %s
            """
            + suffix,
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise RetentionPolicyNotConfigured(
                "The retention policy is not configured. Run the explicit bootstrap command."
            )
        return RetentionPolicy(
            retention_days=row[0],
            legal_hold=row[1],
            policy_version=row[2],
            updated_at=row[3],
        )


_STORE: OperationsStore | None = None
_STORE_LOCK = threading.Lock()


def get_operations_store() -> OperationsStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise OperationsStoreUnavailable(
                "DWAI-ON operations require the configured Agent database."
            )
        _STORE = PostgresOperationsStore(database_url)
        return _STORE


def reset_operations_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
