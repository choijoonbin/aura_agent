from __future__ import annotations

import os
from datetime import timedelta
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import (
    CreateResearchDeliveryRequest,
    ResearchDelivery,
    ResearchDeliveryObservation,
    ResearchDeliveryState,
    ResearchDeliveryType,
    ResearchRunState,
)
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec, advisory_lock
from .personal_domain_security import PersonalDomainIdentity
from .research_run_store import ResearchRunStore, get_research_run_store
from .transactional_outbox import enqueue_internal_intent


class ResearchDeliveryStore:
    def __init__(
        self,
        database_url: str,
        run_store: ResearchRunStore | None = None,
    ) -> None:
        self.database_url = database_url
        self.run_store = run_store or get_research_run_store()
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise DwaionWorkflowUnavailable("Research delivery encryption is unavailable.") from error

    def create(
        self,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        delivery_type: ResearchDeliveryType,
        request: CreateResearchDeliveryRequest,
    ) -> ResearchDelivery:
        run = self.run_store.get(identity, run_id)
        if run.version != request.expected_version:
            raise DwaionWorkflowConflict("The research run version has changed.")
        if run.state != ResearchRunState.COMPLETED or run.receipt_id is None:
            raise DwaionWorkflowConflict("Research delivery requires a completed run receipt.")
        payload = {
            "deliveryType": delivery_type.value,
            "runReceiptId": str(run.receipt_id),
            "parameters": request.parameters,
        }
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(connection, "research-delivery", identity.tenant_id, identity.user_id, request.command_id)
                existing = connection.execute(
                    _SELECT + " WHERE d.tenant_id = %s AND d.user_id = %s AND (d.command_id = %s OR d.idempotency_key = %s)",
                    (identity.tenant_id, identity.user_id, request.command_id, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    stored = self._request(existing)
                    if existing["run_id"] != run_id or existing["delivery_type"] != delivery_type.value or stored != payload:
                        raise DwaionWorkflowConflict("The research delivery key is already bound to another request.")
                    return self._record(existing)
                delivery_id = uuid4()
                state = (
                    ResearchDeliveryState.AWAITING_APPROVAL
                    if delivery_type in {ResearchDeliveryType.HANDOFF, ResearchDeliveryType.SHARE}
                    else ResearchDeliveryState.QUEUED
                )
                request_envelope = self._payload(identity, delivery_id, "request", payload)
                row = connection.execute(
                    """INSERT INTO ai_research_deliveries (
                           delivery_id, run_id, tenant_id, user_id, command_id,
                           idempotency_key, delivery_type, delivery_state, request_envelope)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *""",
                    (
                        delivery_id, run_id, identity.tenant_id, identity.user_id,
                        request.command_id, request.idempotency_key,
                        delivery_type.value, state.value, request_envelope,
                    ),
                ).fetchone()
                enqueue_internal_intent(
                    connection,
                    codec=self.codec,
                    fingerprints=self.fingerprints,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    topic="RESEARCH_DELIVERY",
                    aggregate_type="RESEARCH_DELIVERY",
                    aggregate_id=str(delivery_id),
                    payload={
                        "deliveryId": str(delivery_id),
                        "runId": str(run_id),
                        **payload,
                    },
                    retention_until=row["created_at"] + timedelta(days=30),
                )
                self._event(connection, identity, row, request.command_id, "REQUESTED", None)
                return self._record(row)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research delivery storage is unavailable.") from error

    def list(self, identity: PersonalDomainIdentity, run_id: UUID) -> list[ResearchDelivery]:
        self.run_store.get(identity, run_id)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    _SELECT + " WHERE d.run_id = %s AND d.tenant_id = %s AND d.user_id = %s ORDER BY d.created_at DESC, d.delivery_id DESC",
                    (run_id, identity.tenant_id, identity.user_id),
                ).fetchall()
                return [self._record(row) for row in rows]
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research delivery storage is unavailable.") from error

    def get(
        self, identity: PersonalDomainIdentity, run_id: UUID, delivery_id: UUID
    ) -> ResearchDelivery:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    _SELECT + " WHERE d.delivery_id = %s AND d.run_id = %s AND d.tenant_id = %s AND d.user_id = %s",
                    (delivery_id, run_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if row is None:
                    raise DwaionWorkflowNotFound("The research delivery is unavailable.")
                return self._record(row)
        except DwaionWorkflowNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research delivery storage is unavailable.") from error

    def observe(
        self,
        identity: PersonalDomainIdentity,
        run_id: UUID,
        delivery_id: UUID,
        request: ResearchDeliveryObservation,
    ) -> ResearchDelivery:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    _SELECT + " WHERE d.delivery_id = %s AND d.run_id = %s AND d.tenant_id = %s AND d.user_id = %s FOR UPDATE",
                    (delivery_id, run_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if row is None:
                    raise DwaionWorkflowNotFound("The research delivery is unavailable.")
                replay = connection.execute(
                    "SELECT current_state FROM ai_research_delivery_events WHERE tenant_id = %s AND user_id = %s AND command_id = %s",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if replay is not None:
                    if replay["current_state"] != request.state.value:
                        raise DwaionWorkflowConflict("The research delivery command ID is already in use.")
                    return self._record(row)
                current = ResearchDeliveryState(row["delivery_state"])
                if request.state not in _delivery_targets(current):
                    raise DwaionWorkflowConflict("The research delivery transition is not allowed.")
                receipt_envelope = (
                    self._payload(identity, delivery_id, "receipt", request.receipt or {})
                    if request.receipt_id else None
                )
                updated = connection.execute(
                    """UPDATE ai_research_deliveries
                          SET delivery_state = %s, receipt_id = %s,
                              receipt_envelope = %s,
                              completed_at = CASE WHEN %s = 'COMPLETED' THEN CURRENT_TIMESTAMP ELSE NULL END,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE delivery_id = %s
                    RETURNING *""",
                    (request.state.value, request.receipt_id, receipt_envelope, request.state.value, delivery_id),
                ).fetchone()
                self._event(connection, identity, updated, request.command_id, "STATE_CHANGED", current.value)
                return self._record(updated)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research delivery storage is unavailable.") from error

    @staticmethod
    def _record(row: Any) -> ResearchDelivery:
        return ResearchDelivery(
            delivery_id=row["delivery_id"], run_id=row["run_id"],
            delivery_type=row["delivery_type"], state=row["delivery_state"],
            receipt_id=row["receipt_id"], created_at=row["created_at"],
            updated_at=row["updated_at"], completed_at=row["completed_at"],
        )

    def _request(self, row: Any) -> dict[str, object]:
        return self.codec.decrypt_json(
            row["request_envelope"], tenant_id=row["tenant_id"],
            resource_type="research-delivery", resource_id=str(row["delivery_id"]), field="request",
        )

    def _payload(self, identity: PersonalDomainIdentity, delivery_id: UUID, field: str, payload: dict[str, object]) -> str:
        return self.codec.encrypt_json(
            payload, tenant_id=identity.tenant_id, resource_type="research-delivery",
            resource_id=str(delivery_id), field=field,
        )

    @staticmethod
    def _event(connection: Any, identity: PersonalDomainIdentity, row: Any, command_id: UUID, event_type: str, previous: str | None) -> None:
        connection.execute(
            """INSERT INTO ai_research_delivery_events (
                   event_id, delivery_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state, current_state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(), row["delivery_id"], identity.tenant_id, identity.user_id,
                identity.user_id, identity.correlation_id, command_id, event_type,
                previous, row["delivery_state"],
            ),
        )


def _delivery_targets(current: ResearchDeliveryState) -> set[ResearchDeliveryState]:
    return {
        ResearchDeliveryState.AWAITING_APPROVAL: {ResearchDeliveryState.QUEUED, ResearchDeliveryState.CANCELLED},
        ResearchDeliveryState.QUEUED: {ResearchDeliveryState.RUNNING, ResearchDeliveryState.FAILED, ResearchDeliveryState.CANCELLED},
        ResearchDeliveryState.RUNNING: {ResearchDeliveryState.PARTIAL, ResearchDeliveryState.COMPLETED, ResearchDeliveryState.FAILED},
        ResearchDeliveryState.PARTIAL: {ResearchDeliveryState.RUNNING, ResearchDeliveryState.FAILED, ResearchDeliveryState.CANCELLED},
    }.get(current, set())


@lru_cache(maxsize=1)
def get_research_delivery_store() -> ResearchDeliveryStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise DwaionWorkflowUnavailable("Research delivery storage is unavailable.")
    return ResearchDeliveryStore(database_url)


_SELECT = "SELECT d.* FROM ai_research_deliveries d"
