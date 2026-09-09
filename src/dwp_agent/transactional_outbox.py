from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from psycopg import Connection, connect
from psycopg.rows import dict_row

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedFingerprints,
    GovernedPayloadCodec,
)


@dataclass(frozen=True)
class OutboxLease:
    outbox_id: UUID
    tenant_id: int
    user_id: str
    topic: str
    aggregate_type: str
    aggregate_id: str
    generation: int
    lease_token: UUID
    lease_expires_at: datetime
    payload: dict[str, object]


def enqueue_internal_intent(
    connection: Connection[object],
    *,
    codec: GovernedPayloadCodec,
    fingerprints: GovernedFingerprints,
    tenant_id: int,
    user_id: str,
    topic: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, object],
    retention_until: datetime,
) -> UUID:
    outbox_id = uuid4()
    event_key = fingerprints.value(
        tenant_id=tenant_id,
        purpose=f"outbox:{topic}",
        payload={
            "aggregateType": aggregate_type,
            "aggregateId": aggregate_id,
            "payload": payload,
        },
    )
    envelope = codec.encrypt_json(
        payload,
        tenant_id=tenant_id,
        resource_type="transactional-outbox",
        resource_id=str(outbox_id),
        field="payload",
    )
    inserted = connection.execute(
        """INSERT INTO ai_transactional_outbox (
               outbox_id, tenant_id, user_id, topic, aggregate_type,
               aggregate_id, event_key, payload_envelope, retention_until)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (tenant_id, topic, event_key) DO NOTHING
           RETURNING outbox_id""",
        (
            outbox_id,
            tenant_id,
            user_id,
            topic,
            aggregate_type,
            aggregate_id,
            event_key,
            envelope,
            retention_until,
        ),
    ).fetchone()
    if inserted is not None:
        return inserted[0] if isinstance(inserted, tuple) else inserted["outbox_id"]
    canonical = connection.execute(
        """SELECT outbox_id FROM ai_transactional_outbox
            WHERE tenant_id = %s AND topic = %s AND event_key = %s""",
        (tenant_id, topic, event_key),
    ).fetchone()
    if canonical is None:
        raise GovernedDomainConflict("The canonical outbox intent is unavailable.")
    return canonical[0] if isinstance(canonical, tuple) else canonical["outbox_id"]


class PostgresTransactionalOutboxStore:
    """Fenced delivery bookkeeping only; this class performs no external delivery."""

    def __init__(
        self,
        database_url: str,
        *,
        codec: GovernedPayloadCodec | None = None,
        fingerprints: GovernedFingerprints | None = None,
    ) -> None:
        self.database_url = database_url
        self.codec = codec or GovernedPayloadCodec()
        self.fingerprints = fingerprints or GovernedFingerprints.load()

    def claim(
        self,
        *,
        tenant_id: int,
        topics: tuple[str, ...],
        lease_seconds: int = 30,
    ) -> OutboxLease | None:
        return self._claim(
            tenant_id=tenant_id,
            topics=topics,
            lease_seconds=lease_seconds,
        )

    def claim_any(
        self,
        *,
        topics: tuple[str, ...],
        lease_seconds: int = 30,
    ) -> OutboxLease | None:
        """Claim the next internal intent across tenants for a trusted worker."""
        return self._claim(
            tenant_id=None,
            topics=topics,
            lease_seconds=lease_seconds,
        )

    def _claim(
        self,
        *,
        tenant_id: int | None,
        topics: tuple[str, ...],
        lease_seconds: int,
    ) -> OutboxLease | None:
        if not topics or len(topics) > 20 or not 5 <= lease_seconds <= 300:
            raise ValueError("Outbox claim bounds are invalid.")
        with connect(self.database_url, row_factory=dict_row) as connection:
            tenant_filter = "tenant_id = %s AND " if tenant_id is not None else ""
            parameters: tuple[object, ...] = (
                (tenant_id, list(topics))
                if tenant_id is not None
                else (list(topics),)
            )
            row = connection.execute(
                f"""SELECT outbox_id, tenant_id, user_id, topic, aggregate_type,
                           aggregate_id, payload_envelope, generation
                      FROM ai_transactional_outbox
                     WHERE {tenant_filter}topic = ANY(%s)
                       AND ((state = 'PENDING' AND available_at <= CURRENT_TIMESTAMP)
                         OR (state = 'CLAIMED' AND lease_expires_at <= CURRENT_TIMESTAMP))
                     ORDER BY available_at, created_at, outbox_id
                     FOR UPDATE SKIP LOCKED LIMIT 1""",
                parameters,
            ).fetchone()
            if row is None:
                return None
            claimed_tenant_id = int(row["tenant_id"])
            token = uuid4()
            generation = int(row["generation"]) + 1
            updated = connection.execute(
                """UPDATE ai_transactional_outbox
                      SET state = 'CLAIMED', generation = %s, lease_token = %s,
                          lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                          attempt_count = attempt_count + 1,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE outbox_id = %s
                    RETURNING lease_expires_at""",
                (generation, token, lease_seconds, row["outbox_id"]),
            ).fetchone()
            token_fingerprint = self.fingerprints.value(
                tenant_id=claimed_tenant_id,
                purpose="outbox-lease-token",
                payload={"token": str(token), "generation": generation},
            )
            self._event(
                connection,
                outbox_id=row["outbox_id"],
                tenant_id=claimed_tenant_id,
                event_type="CLAIMED",
                generation=generation,
                token_fingerprint=token_fingerprint,
            )
            payload = self.codec.decrypt_json(
                row["payload_envelope"],
                tenant_id=claimed_tenant_id,
                resource_type="transactional-outbox",
                resource_id=str(row["outbox_id"]),
                field="payload",
            )
            return OutboxLease(
                outbox_id=row["outbox_id"],
                tenant_id=claimed_tenant_id,
                user_id=row["user_id"],
                topic=row["topic"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                generation=generation,
                lease_token=token,
                lease_expires_at=updated["lease_expires_at"],
                payload=payload,
            )

    def acknowledge(self, lease: OutboxLease) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """UPDATE ai_transactional_outbox
                      SET state = 'DELIVERED', lease_token = NULL,
                          lease_expires_at = NULL, delivered_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE outbox_id = %s AND tenant_id = %s AND state = 'CLAIMED'
                      AND generation = %s AND lease_token = %s
                      AND lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING outbox_id""",
                (
                    lease.outbox_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if row is None:
                raise GovernedDomainConflict("The outbox lease is stale or expired.")
            self._event(
                connection,
                outbox_id=lease.outbox_id,
                tenant_id=lease.tenant_id,
                event_type="DELIVERED",
                generation=lease.generation,
            )

    def retry(
        self,
        lease: OutboxLease,
        *,
        safe_error_code: str,
        retry_after_seconds: int = 30,
        maximum_attempts: int = 5,
    ) -> str:
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9_.-]{1,127}", safe_error_code)
            or not 1 <= retry_after_seconds <= 3_600
            or not 1 <= maximum_attempts <= 20
        ):
            raise ValueError("Outbox retry bounds are invalid.")
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT attempt_count FROM ai_transactional_outbox
                    WHERE outbox_id = %s AND tenant_id = %s AND state = 'CLAIMED'
                      AND generation = %s AND lease_token = %s
                      AND lease_expires_at > CURRENT_TIMESTAMP FOR UPDATE""",
                (lease.outbox_id, lease.tenant_id, lease.generation, lease.lease_token),
            ).fetchone()
            if row is None:
                raise GovernedDomainConflict("The outbox lease is stale or expired.")
            dead = int(row["attempt_count"]) >= maximum_attempts
            next_state = "DEAD_LETTER" if dead else "PENDING"
            connection.execute(
                """UPDATE ai_transactional_outbox
                      SET state = %s, lease_token = NULL, lease_expires_at = NULL,
                          available_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                          updated_at = CURRENT_TIMESTAMP
                    WHERE outbox_id = %s""",
                (next_state, retry_after_seconds, lease.outbox_id),
            )
            self._event(
                connection,
                outbox_id=lease.outbox_id,
                tenant_id=lease.tenant_id,
                event_type="DEAD_LETTERED" if dead else "RETRY_SCHEDULED",
                generation=lease.generation,
                safe_error_code=safe_error_code,
            )
            return next_state

    @staticmethod
    def _event(
        connection: Connection[object],
        *,
        outbox_id: UUID,
        tenant_id: int,
        event_type: str,
        generation: int,
        token_fingerprint: str | None = None,
        safe_error_code: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_transactional_outbox_events (
                   event_id, outbox_id, tenant_id, event_type, generation,
                   lease_token_fingerprint, safe_error_code)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                outbox_id,
                tenant_id,
                event_type,
                generation,
                token_fingerprint,
                safe_error_code,
            ),
        )
