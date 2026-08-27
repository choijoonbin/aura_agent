from __future__ import annotations

import csv
import io
import json
import math
import os
import threading
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from psycopg import connect

from .contracts import CitationSourceType, PolicyOutcome
from .governance_bootstrap import bootstrap_governance_policies
from .governance_catalog import SOURCE_DEFINITIONS
from .governance_contracts import (
    ActionPolicy,
    BootstrapGovernancePoliciesRequest,
    ConnectionState,
    DataSourcePolicy,
    GovernanceAuditEvent,
    GovernanceAuditPage,
    SafetyPolicy,
    SourceAccessMode,
    UpdateActionPolicyRequest,
    UpdateDataSourcePolicyRequest,
    UpdateSafetyPolicyRequest,
)
from .governance_errors import (
    GovernancePolicyConflict,
    GovernancePolicyNotInitialized,
    GovernanceStoreUnavailable,
)
from .workplace_actions import all_workplace_actions


MAX_AUDIT_EXPORT_ROWS = 10_000


class GovernanceStore(Protocol):
    def source_policies(self, *, tenant_id: str, actor_user_id: str) -> list[DataSourcePolicy]: ...
    def bootstrap_source_policies(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: BootstrapGovernancePoliciesRequest,
    ) -> list[DataSourcePolicy]: ...
    def update_source_policy(self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
                             source_key: CitationSourceType,
                             request: UpdateDataSourcePolicyRequest) -> DataSourcePolicy: ...
    def action_policies(self, *, tenant_id: str, actor_user_id: str) -> list[ActionPolicy]: ...
    def bootstrap_action_policies(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: BootstrapGovernancePoliciesRequest,
    ) -> list[ActionPolicy]: ...
    def update_action_policy(self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
                             action_key: str,
                             request: UpdateActionPolicyRequest) -> ActionPolicy: ...
    def safety_policy(self, *, tenant_id: str, actor_user_id: str) -> SafetyPolicy: ...
    def bootstrap_safety_policy(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: BootstrapGovernancePoliciesRequest,
    ) -> SafetyPolicy: ...
    def update_safety_policy(self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
                             request: UpdateSafetyPolicyRequest) -> SafetyPolicy: ...
    def audit_events(self, *, tenant_id: str, category: str | None, query: str | None,
                     page: int, size: int) -> GovernanceAuditPage: ...
    def audit_csv(self, *, tenant_id: str, category: str | None,
                  query: str | None) -> tuple[str, bool]: ...


class PostgresGovernanceStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def source_policies(self, *, tenant_id: str, actor_user_id: str) -> list[DataSourcePolicy]:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            rows = connection.execute(
                """SELECT source_key, display_name, description, provider_type,
                          classification, access_mode, enabled, connection_state,
                          connector_ref, policy_version, updated_at
                     FROM ai_data_source_policies
                    WHERE tenant_id = %s ORDER BY source_key""",
                (tenant,),
            ).fetchall()
        return [self._source(row) for row in rows]

    def bootstrap_source_policies(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: BootstrapGovernancePoliciesRequest,
    ) -> list[DataSourcePolicy]:
        tenant = int(tenant_id)
        bootstrap_governance_policies(
            database_url=self.database_url,
            tenant=tenant,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
            request=request,
            category="SOURCE",
            event_type="source-policies.bootstrapped",
            target_type="DATA_SOURCE_POLICY_SET",
            existing_count_sql=(
                "SELECT COUNT(*) FROM ai_data_source_policies WHERE tenant_id = %s"
            ),
            initialize=self._ensure_sources,
        )
        return self.source_policies(tenant_id=tenant_id, actor_user_id=actor_user_id)

    def update_source_policy(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        source_key: CitationSourceType, request: UpdateDataSourcePolicyRequest,
    ) -> DataSourcePolicy:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            current_row = connection.execute(
                """SELECT source_key, display_name, description, provider_type,
                          classification, access_mode, enabled, connection_state,
                          connector_ref, policy_version, updated_at
                     FROM ai_data_source_policies
                    WHERE tenant_id = %s AND source_key = %s FOR UPDATE""",
                (tenant, source_key.value),
            ).fetchone()
            if current_row is None:
                raise GovernancePolicyNotInitialized(
                    "GOVERNANCE_BOOTSTRAP_REQUIRED: The source policy set has not been initialized."
                )
            current = self._source(current_row)
            self._require_version(current.policy_version, request.expected_version)
            state = self._source_state(
                enabled=request.enabled,
                access_mode=request.access_mode,
                connector_ref=request.connector_ref,
                provider_type=current.provider_type,
            )
            row = connection.execute(
                """UPDATE ai_data_source_policies
                      SET classification = %s, access_mode = %s, enabled = %s,
                          connection_state = %s, connector_ref = %s,
                          policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND source_key = %s AND policy_version = %s
                RETURNING source_key, display_name, description, provider_type,
                          classification, access_mode, enabled, connection_state,
                          connector_ref, policy_version, updated_at""",
                (request.classification.value, request.access_mode.value, request.enabled,
                 state.value, request.connector_ref, actor_user_id, tenant,
                 source_key.value, request.expected_version),
            ).fetchone()
            if row is None:
                raise GovernancePolicyConflict("The source policy changed. Reload and retry.")
            updated = self._source(row)
            self._event(connection, tenant, "SOURCE", "source-policy.updated", "DATA_SOURCE",
                        source_key.value, actor_user_id, correlation_id, request.change_reason,
                        current.model_dump(mode="json"), updated.model_dump(mode="json"))
            return updated

    def action_policies(self, *, tenant_id: str, actor_user_id: str) -> list[ActionPolicy]:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            rows = connection.execute(
                """SELECT action_key, enabled, confirmation_required, execution_policy,
                          policy_version, updated_at
                     FROM ai_action_policies WHERE tenant_id = %s ORDER BY action_key""",
                (tenant,),
            ).fetchall()
        definitions = {action.action_key: action for action in all_workplace_actions()}
        return [self._action(row, definitions[row[0]]) for row in rows if row[0] in definitions]

    def bootstrap_action_policies(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: BootstrapGovernancePoliciesRequest,
    ) -> list[ActionPolicy]:
        tenant = int(tenant_id)
        bootstrap_governance_policies(
            database_url=self.database_url,
            tenant=tenant,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
            request=request,
            category="ACTION",
            event_type="action-policies.bootstrapped",
            target_type="ACTION_POLICY_SET",
            existing_count_sql=(
                "SELECT COUNT(*) FROM ai_action_policies WHERE tenant_id = %s"
            ),
            initialize=self._ensure_actions,
        )
        return self.action_policies(tenant_id=tenant_id, actor_user_id=actor_user_id)

    def update_action_policy(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        action_key: str, request: UpdateActionPolicyRequest,
    ) -> ActionPolicy:
        normalized = action_key.strip().upper()
        definitions = {action.action_key: action for action in all_workplace_actions()}
        definition = definitions.get(normalized)
        if definition is None:
            raise KeyError(normalized)
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            current_row = connection.execute(
                """SELECT action_key, enabled, confirmation_required, execution_policy,
                          policy_version, updated_at
                     FROM ai_action_policies
                    WHERE tenant_id = %s AND action_key = %s FOR UPDATE""",
                (tenant, normalized),
            ).fetchone()
            if current_row is None:
                raise GovernancePolicyNotInitialized(
                    "GOVERNANCE_BOOTSTRAP_REQUIRED: The action policy set has not been initialized."
                )
            current = self._action(current_row, definition)
            self._require_version(current.policy_version, request.expected_version)
            row = connection.execute(
                """UPDATE ai_action_policies
                      SET enabled = %s, confirmation_required = %s, execution_policy = %s,
                          policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND action_key = %s AND policy_version = %s
                RETURNING action_key, enabled, confirmation_required, execution_policy,
                          policy_version, updated_at""",
                (request.enabled, request.confirmation_required,
                 request.execution_policy.value, actor_user_id, tenant, normalized,
                 request.expected_version),
            ).fetchone()
            if row is None:
                raise GovernancePolicyConflict("The action policy changed. Reload and retry.")
            updated = self._action(row, definition)
            self._event(connection, tenant, "ACTION", "action-policy.updated", "ACTION",
                        normalized, actor_user_id, correlation_id, request.change_reason,
                        current.model_dump(mode="json"), updated.model_dump(mode="json"))
            return updated

    def safety_policy(self, *, tenant_id: str, actor_user_id: str) -> SafetyPolicy:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            row = connection.execute(self._safety_select(), (tenant,)).fetchone()
        if row is None:
            raise GovernancePolicyNotInitialized(
                "GOVERNANCE_BOOTSTRAP_REQUIRED: The safety policy has not been initialized."
            )
        return self._safety(row)

    def bootstrap_safety_policy(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: BootstrapGovernancePoliciesRequest,
    ) -> SafetyPolicy:
        tenant = int(tenant_id)
        bootstrap_governance_policies(
            database_url=self.database_url,
            tenant=tenant,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
            request=request,
            category="SAFETY",
            event_type="safety-policy.bootstrapped",
            target_type="SAFETY_POLICY_SET",
            existing_count_sql=(
                "SELECT COUNT(*) FROM ai_safety_policies WHERE tenant_id = %s"
            ),
            initialize=self._ensure_safety,
        )
        return self.safety_policy(tenant_id=tenant_id, actor_user_id=actor_user_id)

    def update_safety_policy(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: UpdateSafetyPolicyRequest,
    ) -> SafetyPolicy:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            current_row = connection.execute(
                self._safety_select(" FOR UPDATE"), (tenant,)
            ).fetchone()
            if current_row is None:
                raise GovernancePolicyNotInitialized(
                    "GOVERNANCE_BOOTSTRAP_REQUIRED: The safety policy has not been initialized."
                )
            current = self._safety(current_row)
            self._require_version(current.policy_version, request.expected_version)
            row = connection.execute(
                """UPDATE ai_safety_policies
                      SET privileged_data_outcome = %s, mutation_outcome = %s,
                          require_citations = %s, max_source_scopes = %s, max_tool_calls = %s,
                          policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND policy_version = %s
                RETURNING prompt_injection_outcome, privileged_data_outcome,
                          mutation_outcome, require_citations, public_web_enabled,
                          max_source_scopes, max_tool_calls, policy_version, updated_at""",
                (request.privileged_data_outcome.value, request.mutation_outcome.value,
                 request.require_citations, request.max_source_scopes,
                 request.max_tool_calls, actor_user_id, tenant, request.expected_version),
            ).fetchone()
            if row is None:
                raise GovernancePolicyConflict("The safety policy changed. Reload and retry.")
            updated = self._safety(row)
            self._event(connection, tenant, "SAFETY", "safety-policy.updated", "SAFETY_POLICY",
                        str(tenant), actor_user_id, correlation_id, request.change_reason,
                        current.model_dump(mode="json"), updated.model_dump(mode="json"))
            return updated

    def audit_events(
        self, *, tenant_id: str, category: str | None, query: str | None,
        page: int, size: int,
    ) -> GovernanceAuditPage:
        tenant = int(tenant_id)
        safe_page, safe_size = max(0, page), max(1, min(size, 100))
        clauses, values = ["tenant_id = %s"], [tenant]
        if category:
            clauses.append("category = %s")
            values.append(category.strip().upper())
        if query and query.strip():
            clauses.append("(lower(event_type) LIKE %s OR lower(target_key) LIKE %s)")
            pattern = f"%{query.strip().lower()}%"
            values.extend([pattern, pattern])
        where = " AND ".join(clauses)
        with connect(self.database_url) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM ai_governance_events WHERE {where}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"""SELECT event_id, category, event_type, target_type, target_key,
                           actor_user_id, correlation_id, change_reason, created_at
                      FROM ai_governance_events WHERE {where}
                     ORDER BY created_at DESC, event_id DESC LIMIT %s OFFSET %s""",
                [*values, safe_size, safe_page * safe_size],
            ).fetchall()
        return GovernanceAuditPage(
            content=[GovernanceAuditEvent(
                event_id=row[0], category=row[1], event_type=row[2], target_type=row[3],
                target_key=row[4], actor_user_id=row[5], correlation_id=row[6],
                change_reason=row[7], created_at=row[8]) for row in rows],
            page=safe_page, size=safe_size, total_elements=total,
            total_pages=math.ceil(total / safe_size) if total else 0,
        )

    def audit_csv(
        self, *, tenant_id: str, category: str | None, query: str | None
    ) -> tuple[str, bool]:
        tenant = int(tenant_id)
        clauses, values = ["tenant_id = %s"], [tenant]
        if category:
            clauses.append("category = %s")
            values.append(category.strip().upper())
        if query and query.strip():
            clauses.append("(lower(event_type) LIKE %s OR lower(target_key) LIKE %s)")
            pattern = f"%{query.strip().lower()}%"
            values.extend([pattern, pattern])
        where = " AND ".join(clauses)
        with connect(self.database_url) as connection:
            rows = connection.execute(
                f"""SELECT event_id, category, event_type, target_type, target_key,
                           actor_user_id, correlation_id, change_reason, created_at
                      FROM ai_governance_events WHERE {where}
                     ORDER BY created_at DESC, event_id DESC LIMIT %s""",
                [*values, MAX_AUDIT_EXPORT_ROWS + 1],
            ).fetchall()
        truncated = len(rows) > MAX_AUDIT_EXPORT_ROWS
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["eventId", "category", "eventType", "targetType", "targetKey",
                         "actorUserId", "correlationId", "changeReason", "createdAt"])
        for row in rows[:MAX_AUDIT_EXPORT_ROWS]:
            writer.writerow([_csv_cell(value) for value in [
                row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7] or "",
                row[8].isoformat(),
            ]])
        return buffer.getvalue(), truncated

    def _ensure_sources(self, connection, tenant: int, actor: str) -> int:
        created = 0
        for source_key, (name, description, provider, classification) in SOURCE_DEFINITIONS.items():
            result = connection.execute(
                """INSERT INTO ai_data_source_policies (
                       tenant_id, source_key, display_name, description, provider_type,
                       classification, access_mode, enabled, connection_state, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s, 'BLOCKED', FALSE, 'BLOCKED', %s)
                   ON CONFLICT (tenant_id, source_key) DO NOTHING""",
                (tenant, source_key.value, name, description, provider, classification.value, actor),
            )
            created += result.rowcount
        return created

    def _ensure_actions(self, connection, tenant: int, actor: str) -> int:
        created = 0
        for action in all_workplace_actions():
            result = connection.execute(
                """INSERT INTO ai_action_policies (
                       tenant_id, action_key, enabled, confirmation_required,
                       execution_policy, updated_by)
                   VALUES (%s, %s, FALSE, TRUE, 'BLOCKED', %s)
                   ON CONFLICT (tenant_id, action_key) DO NOTHING""",
                (tenant, action.action_key, actor),
            )
            created += result.rowcount
        return created

    def _ensure_safety(self, connection, tenant: int, actor: str) -> int:
        result = connection.execute(
            """INSERT INTO ai_safety_policies (tenant_id, updated_by)
               VALUES (%s, %s) ON CONFLICT (tenant_id) DO NOTHING""", (tenant, actor))
        return result.rowcount

    def _source(self, row) -> DataSourcePolicy:
        return DataSourcePolicy(
            source_key=row[0], display_name=row[1], description=row[2], provider_type=row[3],
            classification=row[4], access_mode=row[5], enabled=row[6],
            connection_state=row[7], connector_ref=row[8], policy_version=row[9], updated_at=row[10])

    def _action(self, row, definition) -> ActionPolicy:
        return ActionPolicy(
            action_key=row[0], title=definition.title, description=definition.description,
            risk_tier=definition.risk_tier, required_permission=definition.required_permission,
            enabled=row[1], confirmation_required=row[2], execution_policy=row[3],
            policy_version=row[4], updated_at=row[5])

    def _safety(self, row) -> SafetyPolicy:
        return SafetyPolicy(
            prompt_injection_outcome=PolicyOutcome(row[0]),
            privileged_data_outcome=PolicyOutcome(row[1]), mutation_outcome=PolicyOutcome(row[2]),
            require_citations=row[3], public_web_enabled=row[4], max_source_scopes=row[5],
            max_tool_calls=row[6], policy_version=row[7], updated_at=row[8])

    def _safety_select(self, suffix: str = "") -> str:
        return ("SELECT prompt_injection_outcome, privileged_data_outcome, mutation_outcome, "
                "require_citations, public_web_enabled, max_source_scopes, max_tool_calls, "
                "policy_version, updated_at FROM ai_safety_policies WHERE tenant_id = %s" + suffix)

    def _source_state(self, *, enabled: bool, access_mode: SourceAccessMode,
                      connector_ref: str | None, provider_type: str) -> ConnectionState:
        if not enabled or access_mode == SourceAccessMode.BLOCKED:
            return ConnectionState.BLOCKED
        if provider_type.startswith("DWP_") or connector_ref:
            return ConnectionState.CONNECTED
        return ConnectionState.NOT_CONFIGURED

    def _require_version(self, actual: int, expected: int) -> None:
        if actual != expected:
            raise GovernancePolicyConflict("The policy changed. Reload and retry.")

    def _event(self, connection, tenant: int, category: str, event_type: str,
               target_type: str, target_key: str, actor: str, correlation: str,
               reason: str | None, previous: dict | None, current: dict | None) -> None:
        connection.execute(
            """INSERT INTO ai_governance_events (
                   event_id, tenant_id, category, event_type, target_type, target_key,
                   actor_user_id, correlation_id, change_reason, previous_value, current_value)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)""",
            (uuid4(), tenant, category, event_type, target_type, target_key, actor,
             correlation, reason, json.dumps(previous) if previous else None,
             json.dumps(current) if current else None),
        )


_STORE: PostgresGovernanceStore | None = None
_STORE_LOCK = threading.Lock()


def get_governance_store() -> PostgresGovernanceStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise GovernanceStoreUnavailable(
                "DWAI-ON governance requires the configured Agent database.")
        _STORE = PostgresGovernanceStore(database_url)
        return _STORE


def reset_governance_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def _csv_cell(value: object) -> str:
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text
