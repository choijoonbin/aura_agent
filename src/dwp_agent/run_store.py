from __future__ import annotations

import hashlib
import hmac
import os
import threading
from typing import Protocol
from uuid import UUID

from psycopg import connect

from .contracts import AskResponse
from .in_memory_run_store import InMemoryRunStore
from .run_store_types import RunLease, RunStart
from .run_observability import (
    RunStageKey,
    SourceHealthObservation,
    audit_record_id,
)
from .postgres_run_observability import (
    advance_stage as advance_postgres_stage,
    finish_attempt_stage,
    record_source_health as record_postgres_source_health,
    start_stage,
)
from .database_migrations import apply_migrations as _apply_migrations
from .database_migrations import migration_sort_key as _migration_sort_key
from .envelope import PayloadEncryption
from .grounded_response_status import normalize_legacy_ask_response_payload
from .payload_contexts import legacy_run_aad, run_context
from .run_store_errors import RequestIdConflict, RunInProgress, RunStoreUnavailable
from .run_store_crypto import PayloadCipher, load_payload_encryption, load_payload_keyring
from .run_store_lifecycle import (
    database_status,
    initialize_database,
    reset_database_status,
)
from .run_lease_persistence import (
    activate_conversation_messages,
    is_completed_run,
    require_active_run,
)

class RunStore(Protocol):
    def load(
        self,
        tenant_id: str,
        user_id: str,
        request_id: str,
        query_hash: str,
    ) -> AskResponse | None: ...

    def begin(self, start: RunStart) -> RunLease | None: ...

    def require_active(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> None: ...

    def is_completed(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> bool: ...

    def advance_stage(
        self, lease: RunLease, *, tenant_id: str, user_id: str,
        request_id: str, stage: RunStageKey,
    ) -> None: ...

    def record_source_health(
        self, lease: RunLease, *, tenant_id: str, user_id: str,
        request_id: str, observations: tuple[SourceHealthObservation, ...],
    ) -> None: ...

    def complete(
        self,
        response: AskResponse,
        *,
        lease: RunLease,
        tenant_id: str,
        user_id: str,
        provider_request_hash: str | None = None,
    ) -> None: ...

    def fail(self, lease: RunLease, safe_error_code: str) -> None: ...


class PostgresRunStore:
    def __init__(self, database_url: str, encryption: PayloadEncryption) -> None:
        self.database_url = database_url
        self.encryption = encryption

    def load(
        self,
        tenant_id: str,
        user_id: str,
        request_id: str,
        query_hash: str,
    ) -> AskResponse | None:
        with connect(self.database_url) as connection:
            row = connection.execute(
                """
                SELECT run_id, response_nonce, response_ciphertext, run_state, query_hash,
                       response_key_version, response_envelope,
                       lease_expires_at > CURRENT_TIMESTAMP AS lease_active
                  FROM ai_agent_runs
                 WHERE tenant_id = %s AND user_id = %s AND request_id = %s
                """,
                (int(tenant_id), user_id, request_id),
            ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(str(row[4]).strip(), query_hash):
            raise RequestIdConflict("The request ID was already used for another query.")
        if row[3] == "RUNNING" and row[7]:
            raise RunInProgress("An Ask request with this request ID is already running.")
        if row[3] == "RUNNING":
            return None
        if row[6] is None and row[1] is None and row[2] is None:
            return None
        payload = self.encryption.decrypt_bytes(
            envelope=str(row[6]) if row[6] is not None else None,
            context=run_context(tenant_id, str(row[0])),
            legacy_version=str(row[5]) if row[5] is not None else None,
            legacy_nonce=bytes(row[1]) if row[1] is not None else None,
            legacy_ciphertext=bytes(row[2]) if row[2] is not None else None,
            legacy_aad=legacy_run_aad(tenant_id, user_id, request_id, str(row[0])),
        )
        return AskResponse.model_validate_json(normalize_legacy_ask_response_payload(payload))

    def begin(self, start: RunStart) -> RunLease | None:
        with connect(self.database_url) as connection:
            row = connection.execute(
                """
                INSERT INTO ai_agent_runs (
                    run_id, tenant_id, user_id, request_id, query_hash,
                    agent_key, agent_revision, run_state, risk_tier, policy_outcome,
                    locale, correlation_id, lease_expires_at, lease_generation,
                    current_audit_id, audit_record_id, audit_link_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', %s, %s, %s, %s,
                        CURRENT_TIMESTAMP + INTERVAL '2 minutes', 1, %s, %s, %s)
                ON CONFLICT (tenant_id, user_id, request_id) DO NOTHING
                RETURNING run_id, lease_generation
                """,
                (
                    UUID(start.run_id),
                    int(start.tenant_id),
                    start.user_id,
                    start.request_id,
                    start.query_hash,
                    start.agent_key,
                    start.agent_revision,
                    start.risk_tier,
                    start.policy_outcome,
                    start.locale,
                    start.correlation_id,
                    start.audit_id,
                    audit_record_id(start.audit_id) if start.audit_id else None,
                    "PENDING" if start.audit_id else None,
                ),
            ).fetchone()
            if row is not None:
                start_stage(connection, str(row[0]), int(row[1]))
                return RunLease(run_id=str(row[0]), generation=int(row[1]))

            existing = connection.execute(
                """
                SELECT run_id, query_hash, run_state,
                       lease_expires_at > CURRENT_TIMESTAMP AS lease_active
                  FROM ai_agent_runs
                 WHERE tenant_id = %s AND user_id = %s AND request_id = %s
                   FOR UPDATE
                """,
                (int(start.tenant_id), start.user_id, start.request_id),
            ).fetchone()
            if existing is None:
                return None
            if not hmac.compare_digest(str(existing[1]).strip(), start.query_hash):
                raise RequestIdConflict("The request ID was already used for another query.")
            if existing[2] == "RUNNING" and existing[3]:
                return None
            retry = connection.execute(
                """
                UPDATE ai_agent_runs
                   SET agent_key = %s, agent_revision = %s, run_state = 'RUNNING',
                       answer_state = NULL, risk_tier = %s, policy_outcome = %s,
                       status_code = NULL, locale = %s, provider = NULL, model = NULL,
                       input_tokens = 0, output_tokens = 0, total_tokens = 0,
                       latency_ms = 0, source_count = 0, safe_error_code = NULL,
                       correlation_id = %s, response_envelope = NULL,
                       response_key_version = NULL, response_nonce = NULL,
                       response_ciphertext = NULL, completed_at = NULL,
                       lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '2 minutes',
                       lease_generation = lease_generation + 1,
                       current_audit_id = %s, audit_record_id = %s,
                       audit_link_state = %s, data_provenance = 'LIVE'
                 WHERE run_id = %s
                   AND (run_state = 'FAILED'
                        OR (run_state = 'RUNNING' AND lease_expires_at <= CURRENT_TIMESTAMP))
                RETURNING run_id, lease_generation
                """,
                (
                    start.agent_key,
                    start.agent_revision,
                    start.risk_tier,
                    start.policy_outcome,
                    start.locale,
                    start.correlation_id,
                    start.audit_id,
                    audit_record_id(start.audit_id) if start.audit_id else None,
                    "PENDING" if start.audit_id else None,
                    existing[0],
                ),
            ).fetchone()
            if retry is None:
                return None
            start_stage(connection, str(retry[0]), int(retry[1]))
            return RunLease(run_id=str(retry[0]), generation=int(retry[1]))

    def require_active(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> None:
        if not require_active_run(
            self.database_url,
            run_id=lease.run_id,
            generation=lease.generation,
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        ):
            raise RunStoreUnavailable("Agent run lease is no longer owned.")

    def is_completed(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> bool:
        return is_completed_run(
            self.database_url,
            run_id=lease.run_id,
            generation=lease.generation,
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )

    def advance_stage(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
        stage: RunStageKey,
    ) -> None:
        advance_postgres_stage(
            self.database_url, lease, tenant_id=tenant_id, user_id=user_id,
            request_id=request_id, stage=stage,
        )

    def record_source_health(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
        observations: tuple[SourceHealthObservation, ...],
    ) -> None:
        record_postgres_source_health(
            self.database_url, lease, tenant_id=tenant_id, user_id=user_id,
            request_id=request_id, observations=observations,
        )

    def complete(
        self,
        response: AskResponse,
        *,
        lease: RunLease,
        tenant_id: str,
        user_id: str,
        provider_request_hash: str | None = None,
    ) -> None:
        if response.run_id != lease.run_id:
            raise RunStoreUnavailable("Agent response does not match the claimed run lease.")
        envelope = self.encryption.encrypt_bytes(
            response.model_dump_json(by_alias=True).encode("utf-8"),
            run_context(tenant_id, response.run_id),
        )
        route = response.model_route
        record_id = audit_record_id(response.audit_id)
        with connect(self.database_url) as connection:
            updated = connection.execute(
                """
                UPDATE ai_agent_runs
                   SET run_state = %s,
                       answer_state = %s,
                       status_code = %s,
                       provider = %s,
                       model = %s,
                       input_tokens = %s,
                       output_tokens = %s,
                       total_tokens = %s,
                       latency_ms = %s,
                       source_count = %s,
                       response_envelope = %s,
                       response_key_version = NULL,
                       response_nonce = NULL,
                       response_ciphertext = NULL,
                       completed_at = %s, lease_expires_at = NULL,
                       current_audit_id = COALESCE(current_audit_id, %s),
                       audit_record_id = COALESCE(audit_record_id, %s),
                       audit_link_state = 'PENDING'
                 WHERE run_id = %s AND lease_generation = %s
                   AND tenant_id = %s AND user_id = %s
                   AND request_id = %s
                   AND run_state = 'RUNNING'
                   AND lease_expires_at > CURRENT_TIMESTAMP
                   AND (current_audit_id IS NULL OR current_audit_id = %s)
                """,
                (
                    "COMPLETED",
                    response.state,
                    response.status_code,
                    route.provider,
                    route.model,
                    route.input_tokens,
                    route.output_tokens,
                    route.total_tokens,
                    route.latency_ms,
                    response.source_count,
                    envelope,
                    response.completed_at,
                    response.audit_id,
                    record_id,
                    UUID(lease.run_id),
                    lease.generation,
                    int(tenant_id),
                    user_id,
                    response.request_id,
                    response.audit_id,
                ),
            ).rowcount
            if updated != 1:
                raise RunStoreUnavailable("Agent run could not be completed.")
            finish_attempt_stage(connection, lease, RunStageKey.COMPLETED)
            activate_conversation_messages(
                connection,
                run_id=lease.run_id,
                generation=lease.generation,
                completed_at=response.completed_at,
            )
            if route.state != "NOT_INVOKED":
                connection.execute(
                    """
                    INSERT INTO ai_model_calls (
                        run_id, provider, model, call_state, input_tokens,
                        output_tokens, total_tokens, latency_ms, provider_request_hash,
                        safe_error_code)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        UUID(response.run_id),
                        route.provider,
                        route.model,
                        route.state,
                        route.input_tokens,
                        route.output_tokens,
                        route.total_tokens,
                        route.latency_ms,
                        provider_request_hash,
                        None if route.state == "COMPLETED" else response.status_code,
                    ),
                )
            for citation in response.citations:
                connection.execute(
                    """
                    INSERT INTO ai_agent_citations (run_id, source_ref_hash, source_type)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        UUID(response.run_id),
                        _digest(citation.source_id),
                        citation.source_type,
                    ),
                )

    def fail(self, lease: RunLease, safe_error_code: str) -> None:
        with connect(self.database_url) as connection:
            failed = connection.execute(
                """
                UPDATE ai_agent_runs
                   SET run_state = 'FAILED', safe_error_code = %s,
                       completed_at = CURRENT_TIMESTAMP, lease_expires_at = NULL
                 WHERE run_id = %s AND run_state = 'RUNNING'
                   AND lease_generation = %s
                   AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (safe_error_code[:120], UUID(lease.run_id), lease.generation),
            ).rowcount
            if failed == 1:
                finish_attempt_stage(connection, lease, RunStageKey.FAILED)


_STORE: RunStore | None = None
_STORE_LOCK = threading.Lock()


def get_run_store() -> RunStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if database_url:
            _STORE = PostgresRunStore(database_url, load_payload_encryption())
        else:
            _STORE = InMemoryRunStore()
        return _STORE


def reset_run_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
        reset_database_status()


def privacy_hash(value: str) -> str:
    secret = os.getenv("DWP_AGENT_PRIVACY_HASH_SECRET", "").strip()
    if not secret:
        raise RunStoreUnavailable("Agent privacy hash secret is required.")
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
