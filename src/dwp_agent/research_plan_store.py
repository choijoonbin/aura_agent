from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import (
    CreateResearchPlanRequest,
    ResearchPlan,
    ResearchPlanDefinition,
    ResearchPlanState,
    UpdateResearchPlanRequest,
)
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec, advisory_lock
from .personal_domain_security import PersonalDomainIdentity


class ResearchPlanStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise DwaionWorkflowUnavailable("Research plan encryption is unavailable.") from error

    def create(
        self, identity: PersonalDomainIdentity, request: CreateResearchPlanRequest
    ) -> ResearchPlan:
        fingerprint = self._fingerprint(identity, "create", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(connection, "research-plan-command", identity.tenant_id, identity.user_id, request.command_id)
                command = connection.execute(
                    """SELECT plan_id, command_type, request_fingerprint
                         FROM ai_research_plan_commands
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if command is not None:
                    if command["command_type"] != "CREATE" or command["request_fingerprint"] != fingerprint:
                        raise DwaionWorkflowConflict("The research plan command ID is already in use.")
                    return self._get(connection, identity, command["plan_id"])
                plan_id = uuid4()
                definition = request.definition.model_dump(mode="json", by_alias=True)
                envelope = self.codec.encrypt_json(
                    definition,
                    tenant_id=identity.tenant_id,
                    resource_type="research-plan",
                    resource_id=str(plan_id),
                    field="definition",
                )
                definition_fingerprint = self.fingerprints.value(
                    tenant_id=identity.tenant_id,
                    purpose="research-plan-definition",
                    payload=definition,
                )
                connection.execute(
                    """INSERT INTO ai_research_plans (
                           plan_id, tenant_id, user_id, command_id, plan_state,
                           definition_envelope, definition_fingerprint)
                       VALUES (%s, %s, %s, %s, 'READY', %s, %s)""",
                    (plan_id, identity.tenant_id, identity.user_id, request.command_id, envelope, definition_fingerprint),
                )
                self._command(connection, identity, request.command_id, plan_id, "CREATE", fingerprint)
                return self._get(connection, identity, plan_id)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research plan storage is unavailable.") from error

    def update(
        self,
        identity: PersonalDomainIdentity,
        plan_id: UUID,
        request: UpdateResearchPlanRequest,
    ) -> ResearchPlan:
        fingerprint = self._fingerprint(identity, "update", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(connection, "research-plan-command", identity.tenant_id, identity.user_id, request.command_id)
                command = connection.execute(
                    """SELECT plan_id, command_type, request_fingerprint
                         FROM ai_research_plan_commands
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if command is not None:
                    if command["plan_id"] != plan_id or command["command_type"] != "UPDATE" or command["request_fingerprint"] != fingerprint:
                        raise DwaionWorkflowConflict("The research plan command ID is already in use.")
                    return self._get(connection, identity, plan_id)
                row = connection.execute(
                    _SELECT + " WHERE p.plan_id = %s AND p.tenant_id = %s AND p.user_id = %s FOR UPDATE",
                    (plan_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if row is None:
                    raise DwaionWorkflowNotFound("The research plan is unavailable.")
                if int(row["revision"]) != request.expected_revision:
                    raise DwaionWorkflowConflict("The research plan revision has changed.")
                if row["plan_state"] == ResearchPlanState.ARCHIVED.value:
                    raise DwaionWorkflowConflict("An archived research plan cannot be changed.")
                definition = request.definition.model_dump(mode="json", by_alias=True)
                envelope = self.codec.encrypt_json(
                    definition,
                    tenant_id=identity.tenant_id,
                    resource_type="research-plan",
                    resource_id=str(plan_id),
                    field="definition",
                )
                definition_fingerprint = self.fingerprints.value(
                    tenant_id=identity.tenant_id,
                    purpose="research-plan-definition",
                    payload=definition,
                )
                connection.execute(
                    """UPDATE ai_research_plans
                          SET plan_state = 'READY', revision = revision + 1,
                              definition_envelope = %s, definition_fingerprint = %s,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE plan_id = %s""",
                    (envelope, definition_fingerprint, plan_id),
                )
                self._command(connection, identity, request.command_id, plan_id, "UPDATE", fingerprint)
                return self._get(connection, identity, plan_id)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research plan storage is unavailable.") from error

    def get(self, identity: PersonalDomainIdentity, plan_id: UUID) -> ResearchPlan:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                return self._get(connection, identity, plan_id)
        except DwaionWorkflowNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research plan storage is unavailable.") from error

    def _get(self, connection: Any, identity: PersonalDomainIdentity, plan_id: UUID) -> ResearchPlan:
        row = connection.execute(
            _SELECT + " WHERE p.plan_id = %s AND p.tenant_id = %s AND p.user_id = %s",
            (plan_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise DwaionWorkflowNotFound("The research plan is unavailable.")
        definition = self.codec.decrypt_json(
            row["definition_envelope"],
            tenant_id=row["tenant_id"],
            resource_type="research-plan",
            resource_id=str(row["plan_id"]),
            field="definition",
        )
        return ResearchPlan(
            plan_id=row["plan_id"],
            state=row["plan_state"],
            revision=row["revision"],
            definition=ResearchPlanDefinition.model_validate(definition),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _fingerprint(self, identity: PersonalDomainIdentity, operation: str, request: Any) -> str:
        return self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose=f"research-plan:{operation}",
            payload=request.model_dump(mode="json", by_alias=True),
        )

    @staticmethod
    def _command(connection: Any, identity: PersonalDomainIdentity, command_id: UUID, plan_id: UUID, command_type: str, fingerprint: str) -> None:
        connection.execute(
            """INSERT INTO ai_research_plan_commands (
                   tenant_id, user_id, command_id, plan_id, command_type, request_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (identity.tenant_id, identity.user_id, command_id, plan_id, command_type, fingerprint),
        )


@lru_cache(maxsize=1)
def get_research_plan_store() -> ResearchPlanStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise DwaionWorkflowUnavailable("Research plan storage is unavailable.")
    return ResearchPlanStore(database_url)


_SELECT = "SELECT p.* FROM ai_research_plans p"
