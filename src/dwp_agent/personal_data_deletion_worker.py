from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .deletion_job_queries import deletion_result_envelope, read_deletion_job
from .governed_domain_contracts import DomainKey
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec
from .personal_data_disposition import DeletionTargetRejected, purge_domain
from .transactional_outbox import OutboxLease


@dataclass(frozen=True)
class DeletionWorkLease:
    deletion_job_id: UUID
    tenant_id: int
    user_id: str
    generation: int
    lease_token: UUID
    lease_expires_at: datetime
    targets: tuple[DomainKey, ...]


class PostgresPersonalDataDeletionWorker:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.codec = GovernedPayloadCodec()
        self.fingerprints = GovernedFingerprints.load()

    def process(self, outbox_lease: OutboxLease) -> str:
        if (
            outbox_lease.topic != "ai.personal-data.deletion-requested.v1"
            or outbox_lease.aggregate_type != "DATA_DELETION_JOB"
        ):
            raise ValueError("The outbox intent is not a personal-data deletion.")
        deletion_job_id = UUID(outbox_lease.aggregate_id)
        if str(outbox_lease.payload.get("deletionJobId")) != str(deletion_job_id):
            raise DeletionTargetRejected("DELETION_INTENT_BINDING_INVALID")
        lease = self._claim(
            deletion_job_id,
            tenant_id=outbox_lease.tenant_id,
            user_id=outbox_lease.user_id,
        )
        if lease is None:
            return self._state(deletion_job_id, outbox_lease.tenant_id)
        try:
            for domain in lease.targets:
                try:
                    self._purge_target(lease, domain)
                except DeletionTargetRejected as error:
                    self._reject_target(
                        lease, domain, safe_error_code=error.safe_error_code
                    )
            return self._finalize(lease)
        except Exception:
            self.release_for_retry(
                lease, safe_error_code="DELETION_WORKER_RETRY"
            )
            raise

    def release_for_retry(
        self, lease: DeletionWorkLease, *, safe_error_code: str
    ) -> bool:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT attempt_count FROM ai_data_deletion_jobs
                    WHERE deletion_job_id = %s AND tenant_id = %s
                      AND state = 'RUNNING' AND generation = %s
                      AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                    FOR UPDATE""",
                (
                    lease.deletion_job_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if row is None:
                return False
            terminal = int(row["attempt_count"]) >= 5
            target_state = "FAILED" if terminal else "REQUESTED"
            connection.execute(
                """UPDATE ai_data_deletion_targets
                      SET state = %s, safe_error_code = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE deletion_job_id = %s AND state = 'RUNNING'""",
                (target_state, safe_error_code, lease.deletion_job_id),
            )
            job_state = (
                self._terminal_state(connection, lease.deletion_job_id)
                if terminal
                else "REQUESTED"
            )
            completed = "CURRENT_TIMESTAMP" if terminal else "NULL"
            connection.execute(
                f"""UPDATE ai_data_deletion_jobs
                       SET state = %s, lease_token = NULL, lease_expires_at = NULL,
                           completed_at = {completed}, updated_at = CURRENT_TIMESTAMP
                     WHERE deletion_job_id = %s AND tenant_id = %s
                       AND generation = %s AND lease_token = %s""",
                (
                    job_state,
                    lease.deletion_job_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            )
            self._event(
                connection,
                lease,
                "FAILED" if terminal else "RETRY_SCHEDULED",
                "RUNNING",
                job_state,
                safe_error_code,
            )
            self._refresh_result(connection, lease)
            return terminal

    def _claim(
        self, deletion_job_id: UUID, *, tenant_id: int, user_id: str
    ) -> DeletionWorkLease | None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT deletion_job_id, state, generation
                     FROM ai_data_deletion_jobs
                    WHERE deletion_job_id = %s AND tenant_id = %s AND user_id = %s
                    FOR UPDATE""",
                (deletion_job_id, tenant_id, user_id),
            ).fetchone()
            if row is None:
                raise DeletionTargetRejected("DELETION_JOB_NOT_FOUND")
            if row["state"] in {
                "PARTIAL",
                "COMPLETED",
                "BLOCKED_LEGAL_HOLD",
                "FAILED",
            }:
                return None
            held = connection.execute(
                """SELECT target.domain_key
                     FROM ai_data_deletion_targets AS target
                     JOIN ai_domain_retention_policies AS policy
                       ON policy.tenant_id = %s
                      AND policy.domain_key = target.domain_key
                    WHERE target.deletion_job_id = %s
                      AND target.state <> 'COMPLETED' AND policy.legal_hold""",
                (tenant_id, deletion_job_id),
            ).fetchall()
            held_domains = [item["domain_key"] for item in held]
            if held_domains:
                connection.execute(
                    """UPDATE ai_data_deletion_targets
                          SET state = 'BLOCKED_LEGAL_HOLD',
                              safe_error_code = 'LEGAL_HOLD_ACTIVE',
                              updated_at = CURRENT_TIMESTAMP
                        WHERE deletion_job_id = %s AND domain_key = ANY(%s)
                          AND state <> 'COMPLETED'""",
                    (deletion_job_id, held_domains),
                )
            targets = connection.execute(
                """SELECT domain_key FROM ai_data_deletion_targets
                    WHERE deletion_job_id = %s
                      AND state IN ('REQUESTED', 'FAILED')
                    ORDER BY CASE domain_key
                        WHEN 'ARTIFACT_EXPORT' THEN 1
                        WHEN 'ARTIFACT' THEN 2
                        WHEN 'ROUTINE' THEN 3 ELSE 4 END""",
                (deletion_job_id,),
            ).fetchall()
            if not targets:
                terminal_state = self._terminal_state(connection, deletion_job_id)
                connection.execute(
                    """UPDATE ai_data_deletion_jobs
                          SET state = %s, completed_at = CURRENT_TIMESTAMP,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE deletion_job_id = %s""",
                    (terminal_state, deletion_job_id),
                )
                return None
            token = uuid4()
            generation = int(row["generation"]) + 1
            claimed = connection.execute(
                """UPDATE ai_data_deletion_jobs
                      SET state = 'RUNNING', generation = %s, lease_token = %s,
                          lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '120 seconds',
                          attempt_count = attempt_count + 1,
                          started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                          completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE deletion_job_id = %s
                      AND (state = 'REQUESTED'
                        OR (state = 'RUNNING' AND lease_expires_at <= CURRENT_TIMESTAMP))
                    RETURNING lease_expires_at""",
                (generation, token, deletion_job_id),
            ).fetchone()
            if claimed is None:
                raise RuntimeError("The deletion job already has a live lease.")
            domains = tuple(DomainKey(item["domain_key"]) for item in targets)
            connection.execute(
                """UPDATE ai_data_deletion_targets
                      SET state = 'RUNNING', safe_error_code = NULL,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE deletion_job_id = %s AND domain_key = ANY(%s)""",
                (deletion_job_id, [domain.value for domain in domains]),
            )
            lease = DeletionWorkLease(
                deletion_job_id=deletion_job_id,
                tenant_id=tenant_id,
                user_id=user_id,
                generation=generation,
                lease_token=token,
                lease_expires_at=claimed["lease_expires_at"],
                targets=domains,
            )
            self._event(
                connection, lease, "CLAIMED", row["state"], "RUNNING", None
            )
            return lease

    def _purge_target(self, lease: DeletionWorkLease, domain: DomainKey) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            self._require_lease(connection, lease)
            self._require_target(connection, lease, domain)
            policy = connection.execute(
                """SELECT legal_hold FROM ai_domain_retention_policies
                    WHERE tenant_id = %s AND domain_key = %s FOR SHARE""",
                (lease.tenant_id, domain.value),
            ).fetchone()
            if policy is None:
                raise DeletionTargetRejected("RETENTION_POLICY_MISSING")
            if policy["legal_hold"]:
                raise DeletionTargetRejected("LEGAL_HOLD_ACTIVE")
            self._set_disposition_context(connection, lease, domain)
            counts = purge_domain(connection, lease, domain)
            count = sum(counts.values())
            disposition_id = uuid4()
            receipt_payload = {
                "deletionJobId": str(lease.deletion_job_id),
                "domain": domain.value,
                "generation": lease.generation,
                "purgedRowCount": count,
                "tableCounts": counts,
                "dispositionScope": "AGENT_ACTIVE_POSTGRES_DOMAIN_ONLY",
                "dispositionMethod": "PHYSICAL_ROW_PURGE_OF_ENCRYPTED_RECORDS",
                "activeStoreEnvelopesDestroyed": True,
                "sourceSystemDataAffected": False,
                "backupDispositionState": "EXTERNAL_RETENTION_BOUNDARY",
            }
            fingerprint = self.fingerprints.value(
                tenant_id=lease.tenant_id,
                purpose="personal-data-disposition-receipt",
                payload=receipt_payload,
            )
            connection.execute(
                """INSERT INTO ai_data_disposition_receipts (
                       disposition_id, deletion_job_id, tenant_id, user_id,
                       domain_key, generation, purged_row_count, table_counts,
                       active_store_envelopes_destroyed,
                       source_system_data_affected, backup_disposition_state,
                       receipt_fingerprint)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           TRUE, FALSE, 'EXTERNAL_RETENTION_BOUNDARY', %s)""",
                (
                    disposition_id,
                    lease.deletion_job_id,
                    lease.tenant_id,
                    lease.user_id,
                    domain.value,
                    lease.generation,
                    count,
                    Jsonb(counts),
                    fingerprint,
                ),
            )
            updated = connection.execute(
                """UPDATE ai_data_deletion_targets
                      SET state = 'COMPLETED', affected_count = %s,
                          disposition_id = %s, completed_at = CURRENT_TIMESTAMP,
                          safe_error_code = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE deletion_job_id = %s AND domain_key = %s
                      AND state = 'RUNNING'
                    RETURNING disposition_id""",
                (count, disposition_id, lease.deletion_job_id, domain.value),
            ).fetchone()
            if updated is None:
                raise RuntimeError("The deletion target lease was lost before commit.")
            self._event(
                connection,
                lease,
                "TARGET_COMPLETED",
                "RUNNING",
                "RUNNING",
                None,
            )

    def _reject_target(
        self, lease: DeletionWorkLease, domain: DomainKey, *, safe_error_code: str
    ) -> None:
        target_state = (
            "BLOCKED_LEGAL_HOLD"
            if safe_error_code == "LEGAL_HOLD_ACTIVE"
            else "FAILED"
        )
        with connect(self.database_url) as connection:
            self._require_lease(connection, lease)
            self._require_target(connection, lease, domain)
            updated = connection.execute(
                """UPDATE ai_data_deletion_targets
                      SET state = %s, safe_error_code = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE deletion_job_id = %s AND domain_key = %s
                      AND state = 'RUNNING'""",
                (
                    target_state,
                    safe_error_code,
                    lease.deletion_job_id,
                    domain.value,
                ),
            ).rowcount
            if updated != 1:
                raise RuntimeError("The deletion target lease was lost before rejection.")
            self._event(
                connection,
                lease,
                "TARGET_BLOCKED" if target_state == "BLOCKED_LEGAL_HOLD" else "TARGET_FAILED",
                "RUNNING",
                "RUNNING",
                safe_error_code,
            )

    def _finalize(self, lease: DeletionWorkLease) -> str:
        with connect(self.database_url, row_factory=dict_row) as connection:
            self._require_lease(connection, lease)
            state = self._terminal_state(connection, lease.deletion_job_id)
            connection.execute(
                """UPDATE ai_data_deletion_jobs
                      SET state = %s, lease_token = NULL, lease_expires_at = NULL,
                          completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE deletion_job_id = %s AND tenant_id = %s
                      AND generation = %s AND lease_token = %s""",
                (
                    state,
                    lease.deletion_job_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            )
            self._event(connection, lease, "COMPLETED", "RUNNING", state, None)
            self._refresh_result(connection, lease)
            return state

    @staticmethod
    def _terminal_state(connection: Any, deletion_job_id: UUID) -> str:
        states = [
            row["state"]
            for row in connection.execute(
                """SELECT state FROM ai_data_deletion_targets
                    WHERE deletion_job_id = %s""",
                (deletion_job_id,),
            ).fetchall()
        ]
        if states and all(state == "COMPLETED" for state in states):
            return "COMPLETED"
        if "COMPLETED" in states:
            return "PARTIAL"
        if states and all(state == "BLOCKED_LEGAL_HOLD" for state in states):
            return "BLOCKED_LEGAL_HOLD"
        return "FAILED"

    @staticmethod
    def _set_disposition_context(
        connection: Any, lease: DeletionWorkLease, domain: DomainKey
    ) -> None:
        for key, value in (
            ("dwp.disposition_job_id", str(lease.deletion_job_id)),
            ("dwp.disposition_generation", str(lease.generation)),
            ("dwp.disposition_lease_token", str(lease.lease_token)),
            ("dwp.disposition_domain", domain.value),
        ):
            connection.execute("SELECT set_config(%s, %s, TRUE)", (key, value))

    @staticmethod
    def _require_target(
        connection: Any, lease: DeletionWorkLease, domain: DomainKey
    ) -> None:
        row = connection.execute(
            """SELECT 1 FROM ai_data_deletion_targets
                WHERE deletion_job_id = %s AND domain_key = %s
                  AND state = 'RUNNING' FOR UPDATE""",
            (lease.deletion_job_id, domain.value),
        ).fetchone()
        if row is None:
            raise RuntimeError("The deletion target lease is stale or terminal.")

    @staticmethod
    def _require_lease(connection: Any, lease: DeletionWorkLease) -> None:
        row = connection.execute(
            """SELECT 1 FROM ai_data_deletion_jobs
                WHERE deletion_job_id = %s AND tenant_id = %s AND user_id = %s
                  AND state = 'RUNNING' AND generation = %s AND lease_token = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP FOR UPDATE""",
            (
                lease.deletion_job_id,
                lease.tenant_id,
                lease.user_id,
                lease.generation,
                lease.lease_token,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("The deletion lease is stale or expired.")

    def _refresh_result(self, connection: Any, lease: DeletionWorkLease) -> None:
        result = read_deletion_job(
            connection,
            deletion_job_id=lease.deletion_job_id,
            tenant_id=lease.tenant_id,
            user_id=lease.user_id,
            fingerprints=self.fingerprints,
        )
        connection.execute(
            """UPDATE ai_data_deletion_jobs SET result_envelope = %s
                WHERE deletion_job_id = %s AND tenant_id = %s""",
            (
                deletion_result_envelope(
                    self.codec, result, tenant_id=lease.tenant_id
                ),
                lease.deletion_job_id,
                lease.tenant_id,
            ),
        )

    def _state(self, deletion_job_id: UUID, tenant_id: int) -> str:
        with connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT state FROM ai_data_deletion_jobs
                    WHERE deletion_job_id = %s AND tenant_id = %s""",
                (deletion_job_id, tenant_id),
            ).fetchone()
        if row is None:
            raise DeletionTargetRejected("DELETION_JOB_NOT_FOUND")
        return str(row[0])

    @staticmethod
    def _event(
        connection: Any,
        lease: DeletionWorkLease,
        event_type: str,
        previous_state: str | None,
        current_state: str,
        safe_error_code: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_data_deletion_events (
                   event_id, deletion_job_id, tenant_id, user_id, actor_user_id,
                   correlation_id, event_type, previous_state, current_state,
                   generation, safe_error_code)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                lease.deletion_job_id,
                lease.tenant_id,
                lease.user_id,
                "SYSTEM_DISPOSITION_WORKER",
                str(lease.deletion_job_id),
                event_type,
                previous_state,
                current_state,
                lease.generation,
                safe_error_code,
            ),
        )
