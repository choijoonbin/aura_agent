from __future__ import annotations

from typing import Callable, TypeVar
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError
from psycopg import connect
from pydantic import ValidationError

from .envelope import EnvelopeEncryptionError, KeyContext
from .proposal_analysis_commands import (
    ANALYSIS_PURPOSE,
    LEASE_TTL,
    MAX_ANALYSES_PER_DAY,
    MAX_ANALYSES_PER_MINUTE,
    MAX_COMMAND_ATTEMPTS,
    ProposalAnalysisCommandUnavailable,
    ProposalAnalysisInProgress,
    ProposalAnalysisLease,
    ProposalAnalysisRateLimited,
    ProposalAnalysisReplay,
    ProposalAnalysisStart,
)
from .proposal_analysis_contracts import ProposalAnalysisReceipt
from .proposal_analysis_control import ProposalAnalysisDisabled
from .proposal_analysis_fingerprints import ProposalAnalysisFingerprints
from .run_store_crypto import load_payload_encryption
from .run_store_errors import RunStoreUnavailable


_Result = TypeVar("_Result")


class PostgresProposalAnalysisCommandStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.fingerprints = ProposalAnalysisFingerprints.load()
        try:
            self.encryption = load_payload_encryption()
        except RunStoreUnavailable as error:
            raise ProposalAnalysisCommandUnavailable(
                "Proposal analysis command storage is unavailable."
            ) from error

    def begin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        auth_session_id: str,
        command_id: UUID,
        locale: str,
    ) -> ProposalAnalysisStart:
        tenant = _tenant(tenant_id)
        session = self.fingerprints.session(tenant, auth_session_id)
        request = self.fingerprints.analysis_command(tenant, locale=locale)
        try:
            with connect(self.database_url) as connection:
                _lock_user(connection, tenant, user_id)
                self._require_enabled(connection, tenant, user_id)
                row = connection.execute(
                    """SELECT session_fingerprint, request_fingerprint, status,
                              generation, lease_token, attempt_count, started_at,
                              result_envelope
                         FROM ai_agent_proposal_analysis_commands
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s
                        FOR UPDATE""",
                    (tenant, user_id, command_id),
                ).fetchone()
                if row is not None:
                    return self._resume(
                        connection,
                        row,
                        tenant=tenant,
                        user_id=user_id,
                        command_id=command_id,
                        auth_session_id=auth_session_id,
                        locale=locale,
                    )
                counts = connection.execute(
                    """SELECT
                           COUNT(*) FILTER (
                               WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '1 minute'),
                           COUNT(*) FILTER (
                               WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '1 day')
                         FROM ai_agent_proposal_analysis_commands
                        WHERE tenant_id = %s AND user_id = %s""",
                    (tenant, user_id),
                ).fetchone()
                if (
                    counts[0] >= MAX_ANALYSES_PER_MINUTE
                    or counts[1] >= MAX_ANALYSES_PER_DAY
                ):
                    raise ProposalAnalysisRateLimited(
                        "Analysis request capacity exceeded."
                    )
                token = uuid4()
                connection.execute(
                    """INSERT INTO ai_agent_proposal_analysis_commands (
                           tenant_id, user_id, command_id, purpose,
                           session_fingerprint, request_fingerprint, status,
                           generation, lease_token, attempt_count, started_at)
                       VALUES (%s, %s, %s, %s, %s, %s, 'RUNNING', 1, %s, 1,
                               CURRENT_TIMESTAMP)""",
                    (
                        tenant,
                        user_id,
                        command_id,
                        ANALYSIS_PURPOSE,
                        session,
                        request,
                        token,
                    ),
                )
                self._prune(connection, tenant, user_id)
                return ProposalAnalysisStart(
                    lease=ProposalAnalysisLease(command_id, 1, token)
                )
        except (
            ProposalAnalysisDisabled,
            ProposalAnalysisInProgress,
            ProposalAnalysisRateLimited,
            ProposalAnalysisReplay,
        ):
            raise
        except (
            PsycopgError,
            EnvelopeEncryptionError,
            ValidationError,
            ValueError,
        ) as error:
            raise ProposalAnalysisCommandUnavailable(
                "Proposal analysis command storage is unavailable."
            ) from error

    def _resume(
        self,
        connection,
        row,
        *,
        tenant: int,
        user_id: str,
        command_id: UUID,
        auth_session_id: str,
        locale: str,
    ) -> ProposalAnalysisStart:
        self._validate_replay(
            row,
            tenant=tenant,
            auth_session_id=auth_session_id,
            locale=locale,
        )
        if row[2] == "COMPLETED" and row[7] is not None:
            return ProposalAnalysisStart(
                replay=self._decrypt_receipt(tenant, command_id, str(row[7]))
            )
        now = connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        if row[2] == "RUNNING" and row[6] > now - LEASE_TTL:
            raise ProposalAnalysisInProgress(
                "The analysis command is already running."
            )
        if int(row[5]) >= MAX_COMMAND_ATTEMPTS:
            raise ProposalAnalysisRateLimited(
                "The analysis command retry limit was reached."
            )
        token = uuid4()
        generation = int(row[3]) + 1
        connection.execute(
            """UPDATE ai_agent_proposal_analysis_commands
                  SET status = 'RUNNING', generation = %s,
                      lease_token = %s, attempt_count = attempt_count + 1,
                      started_at = CURRENT_TIMESTAMP,
                      updated_at = CURRENT_TIMESTAMP,
                      completed_at = NULL, result_envelope = NULL
                WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
            (generation, token, tenant, user_id, command_id),
        )
        return ProposalAnalysisStart(
            lease=ProposalAnalysisLease(command_id, generation, token)
        )

    def complete(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
        receipt: ProposalAnalysisReceipt,
    ) -> ProposalAnalysisReceipt:
        tenant = _tenant(tenant_id)
        try:
            envelope = self.encryption.encrypt_bytes(
                receipt.model_dump_json(by_alias=True).encode("utf-8"),
                _result_context(tenant, lease.command_id),
            )
            with connect(self.database_url) as connection:
                updated = connection.execute(
                    """UPDATE ai_agent_proposal_analysis_commands
                          SET status = 'COMPLETED', lease_token = NULL,
                              result_envelope = %s, completed_at = CURRENT_TIMESTAMP,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s
                          AND status = 'RUNNING' AND generation = %s
                          AND lease_token = %s""",
                    (
                        envelope,
                        tenant,
                        user_id,
                        lease.command_id,
                        lease.generation,
                        lease.token,
                    ),
                ).rowcount
                if updated != 1:
                    raise ProposalAnalysisReplay(
                        "The analysis command lease is no longer current."
                    )
                return receipt
        except ProposalAnalysisReplay:
            raise
        except (
            PsycopgError,
            EnvelopeEncryptionError,
            ValidationError,
            ValueError,
        ) as error:
            raise ProposalAnalysisCommandUnavailable(
                "Proposal analysis command storage is unavailable."
            ) from error

    def fail(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
    ) -> None:
        try:
            with connect(self.database_url) as connection:
                connection.execute(
                    """UPDATE ai_agent_proposal_analysis_commands
                          SET status = 'FAILED', lease_token = NULL,
                              result_envelope = NULL, updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s
                          AND status = 'RUNNING' AND generation = %s
                          AND lease_token = %s""",
                    (
                        _tenant(tenant_id),
                        user_id,
                        lease.command_id,
                        lease.generation,
                        lease.token,
                    ),
                )
        except (PsycopgError, ValueError) as error:
            raise ProposalAnalysisCommandUnavailable(
                "Proposal analysis command storage is unavailable."
            ) from error

    def clear_for_user(self, *, tenant_id: str, user_id: str) -> None:
        try:
            with connect(self.database_url) as connection:
                connection.execute(
                    """UPDATE ai_agent_proposal_analysis_commands
                          SET status = 'FAILED', generation = generation + 1,
                              lease_token = NULL, completed_at = NULL,
                              result_envelope = NULL,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND user_id = %s""",
                    (_tenant(tenant_id), user_id),
                )
        except (PsycopgError, ValueError) as error:
            raise ProposalAnalysisCommandUnavailable(
                "Proposal analysis command storage is unavailable."
            ) from error

    def run_under_lease(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
        operation: Callable[[], _Result],
    ) -> _Result:
        tenant = _tenant(tenant_id)
        try:
            with connect(self.database_url) as connection:
                _lock_user(connection, tenant, user_id)
                current = connection.execute(
                    """SELECT 1
                         FROM ai_agent_proposal_analysis_commands
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s
                          AND status = 'RUNNING' AND generation = %s
                          AND lease_token = %s
                        FOR UPDATE""",
                    (
                        tenant,
                        user_id,
                        lease.command_id,
                        lease.generation,
                        lease.token,
                    ),
                ).fetchone()
                if current is None:
                    raise ProposalAnalysisReplay(
                        "The analysis command lease is no longer current."
                    )
                return operation()
        except ProposalAnalysisReplay:
            raise
        except (PsycopgError, ValueError) as error:
            raise ProposalAnalysisCommandUnavailable(
                "Proposal analysis command storage is unavailable."
            ) from error

    def _validate_replay(
        self,
        row,
        *,
        tenant: int,
        auth_session_id: str,
        locale: str,
    ) -> None:
        if not self.fingerprints.matches_session(
            str(row[0]), tenant, auth_session_id
        ):
            raise ProposalAnalysisReplay(
                "The analysis command belongs to another session."
            )
        if not self.fingerprints.matches_analysis_command(
            str(row[1]), tenant, locale=locale
        ):
            raise ProposalAnalysisReplay(
                "The analysis command payload changed."
            )

    def _decrypt_receipt(
        self, tenant: int, command_id: UUID, envelope: str
    ) -> ProposalAnalysisReceipt:
        payload = self.encryption.decrypt_bytes(
            envelope=envelope,
            context=_result_context(tenant, command_id),
            legacy_version=None,
            legacy_nonce=None,
            legacy_ciphertext=None,
            legacy_aad=b"",
        )
        return ProposalAnalysisReceipt.model_validate_json(payload)

    def _require_enabled(self, connection, tenant: int, user_id: str) -> None:
        row = connection.execute(
            """SELECT proactive_analysis_enabled
                 FROM ai_agent_proposal_preferences
                WHERE tenant_id = %s AND user_id = %s""",
            (tenant, user_id),
        ).fetchone()
        if row is not None and not row[0]:
            raise ProposalAnalysisDisabled("Proactive analysis is disabled.")

    def _prune(self, connection, tenant: int, user_id: str) -> None:
        connection.execute(
            """DELETE FROM ai_agent_proposal_analysis_commands
                WHERE tenant_id = %s AND user_id = %s
                  AND ((status = 'FAILED'
                        AND updated_at < CURRENT_TIMESTAMP - INTERVAL '2 days')
                    OR (status = 'COMPLETED'
                        AND completed_at < CURRENT_TIMESTAMP - INTERVAL '90 days'))""",
            (tenant, user_id),
        )


def _tenant(value: str) -> int:
    tenant = int(value)
    if tenant <= 0:
        raise ValueError("Tenant must be positive.")
    return tenant


def _lock_user(connection, tenant: int, user_id: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"dwp-agent:proposal-analysis:{tenant}:{user_id}",),
    )


def _result_context(tenant_id: int, command_id: UUID) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="proposal-analysis-command",
        resource_id=str(command_id),
        field="result",
    )


__all__ = ["PostgresProposalAnalysisCommandStore"]
