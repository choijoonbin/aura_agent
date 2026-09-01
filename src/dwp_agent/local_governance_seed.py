from __future__ import annotations

import json
import os
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from psycopg import connect

from .governance_catalog import SOURCE_DEFINITIONS
from .key_provider import normalized_environment
from .workplace_actions import all_workplace_actions


class LocalGovernanceSeedConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalGovernanceSeedResult:
    tenant_id: int
    source_count: int
    action_count: int
    safety_count: int
    retention_count: int


def seed_local_governance() -> tuple[LocalGovernanceSeedResult, ...]:
    if not _enabled("DWP_AGENT_LOCAL_GOVERNANCE_SEED_ENABLED"):
        return ()
    if normalized_environment() != "local":
        raise LocalGovernanceSeedConfigurationError(
            "Local Agent governance seed is allowed only in DWP_ENVIRONMENT=local."
        )

    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise LocalGovernanceSeedConfigurationError(
            "Local Agent governance seed requires DWP_AGENT_DATABASE_URL."
        )
    tenant_ids = _tenant_ids(os.getenv("DWP_AGENT_LOCAL_GOVERNANCE_TENANT_IDS", ""))
    if not tenant_ids:
        raise LocalGovernanceSeedConfigurationError(
            "Local Agent governance seed requires at least one positive tenant ID."
        )

    return tuple(_seed_tenant(database_url, tenant_id) for tenant_id in tenant_ids)


def _seed_tenant(database_url: str, tenant_id: int) -> LocalGovernanceSeedResult:
    source_count = len(SOURCE_DEFINITIONS)
    actions = all_workplace_actions()
    action_count = len(actions)
    actor = "local-governance-seed"
    correlation_id = f"local-governance-seed-{tenant_id}"

    with connect(database_url) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"dwp-agent:local-governance:{tenant_id}",),
        )
        existing_sources = _count(connection, "ai_data_source_policies", tenant_id)
        existing_actions = _count(connection, "ai_action_policies", tenant_id)
        existing_safety = _count(connection, "ai_safety_policies", tenant_id)
        existing_retention = _count(
            connection, "ai_conversation_retention_policies", tenant_id
        )
        _require_complete("source", existing_sources, source_count)
        _require_complete("action", existing_actions, action_count)
        _require_complete("safety", existing_safety, 1)
        _require_complete("retention", existing_retention, 1)

        if existing_sources == 0:
            for source_key, definition in SOURCE_DEFINITIONS.items():
                name, description, provider, classification = definition
                connection.execute(
                    """INSERT INTO ai_data_source_policies (
                           tenant_id, source_key, display_name, description, provider_type,
                           classification, access_mode, enabled, connection_state, updated_by)
                       VALUES (%s, %s, %s, %s, %s, %s,
                               'SOURCE_PERMISSIONS', TRUE, 'CONNECTED', %s)""",
                    (
                        tenant_id,
                        source_key.value,
                        name,
                        description,
                        provider,
                        classification.value,
                        actor,
                    ),
                )
            _audit(
                connection,
                tenant_id=tenant_id,
                category="SOURCE",
                target_key="SOURCE:LOCAL_SEED:v1",
                actor=actor,
                correlation_id=correlation_id,
                current={"createdCount": source_count, "accessMode": "SOURCE_PERMISSIONS"},
            )

        if existing_actions == 0:
            for action in actions:
                connection.execute(
                    """INSERT INTO ai_action_policies (
                           tenant_id, action_key, enabled, confirmation_required,
                           execution_policy, updated_by)
                       VALUES (%s, %s, FALSE, TRUE, 'BLOCKED', %s)""",
                    (tenant_id, action.action_key, actor),
                )
            _audit(
                connection,
                tenant_id=tenant_id,
                category="ACTION",
                target_key="ACTION:LOCAL_SEED:v1",
                actor=actor,
                correlation_id=correlation_id,
                current={"createdCount": action_count, "executionPolicy": "BLOCKED"},
            )

        if existing_safety == 0:
            connection.execute(
                "INSERT INTO ai_safety_policies (tenant_id, updated_by) VALUES (%s, %s)",
                (tenant_id, actor),
            )
            _audit(
                connection,
                tenant_id=tenant_id,
                category="SAFETY",
                target_key="SAFETY:LOCAL_SEED:v1",
                actor=actor,
                correlation_id=correlation_id,
                current={"createdCount": 1, "publicWebEnabled": False},
            )

        if existing_retention == 0:
            connection.execute(
                """INSERT INTO ai_conversation_retention_policies (
                       tenant_id, retention_days, legal_hold)
                   VALUES (%s, 90, FALSE)""",
                (tenant_id,),
            )
            _audit(
                connection,
                tenant_id=tenant_id,
                category="RETENTION",
                target_key="RETENTION:LOCAL_SEED:v1",
                actor=actor,
                correlation_id=correlation_id,
                current={"createdCount": 1, "retentionDays": 90, "legalHold": False},
            )

    return LocalGovernanceSeedResult(
        tenant_id=tenant_id,
        source_count=source_count,
        action_count=action_count,
        safety_count=1,
        retention_count=1,
    )


def _count(connection, table: str, tenant_id: int) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]
    )


def _require_complete(label: str, actual: int, expected: int) -> None:
    if actual not in {0, expected}:
        raise LocalGovernanceSeedConfigurationError(
            f"Local Agent {label} policy set is partial ({actual}/{expected})."
        )


def _audit(
    connection,
    *,
    tenant_id: int,
    category: str,
    target_key: str,
    actor: str,
    correlation_id: str,
    current: dict[str, object],
) -> None:
    event_id = uuid5(NAMESPACE_URL, f"dwp://local-governance/{tenant_id}/{category}")
    connection.execute(
        """INSERT INTO ai_governance_events (
               event_id, tenant_id, category, event_type, target_type, target_key,
               actor_user_id, correlation_id, change_reason, previous_value, current_value)
           VALUES (%s, %s, %s, 'local-governance.seeded', 'POLICY_SET', %s,
                   %s, %s, %s, %s::jsonb, %s::jsonb)""",
        (
            event_id,
            tenant_id,
            category,
            target_key,
            actor,
            correlation_id,
            "Initialize deterministic local-only Agent governance fixtures.",
            json.dumps({"existingCount": 0}),
            json.dumps(current),
        ),
    )


def _tenant_ids(value: str) -> tuple[int, ...]:
    if not value.strip():
        return ()
    try:
        parsed = tuple(int(item.strip()) for item in value.split(","))
    except ValueError as error:
        raise LocalGovernanceSeedConfigurationError(
            "Local Agent governance tenant IDs must be positive integers."
        ) from error
    if any(tenant_id <= 0 for tenant_id in parsed) or len(set(parsed)) != len(parsed):
        raise LocalGovernanceSeedConfigurationError(
            "Local Agent governance tenant IDs must be unique positive integers."
        )
    return parsed


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}
