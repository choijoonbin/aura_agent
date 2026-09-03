from __future__ import annotations

import hashlib
import hmac
import os
import threading
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from psycopg import connect

from .contracts import AskResponse
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

@dataclass(frozen=True)
class RunStart:
    run_id: str
    tenant_id: str
    user_id: str
    request_id: str
    query_hash: str
    agent_key: str
    agent_revision: int
    risk_tier: str
    policy_outcome: str
    locale: str
    correlation_id: str


@dataclass(frozen=True)
class RunLease:
    run_id: str
    generation: int


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


class InMemoryRunStore:
    def __init__(self) -> None:
        self._responses: dict[tuple[str, str, str], AskResponse] = {}
        self._pending: dict[tuple[str, str, str], RunLease] = {}
        self._run_keys: dict[str, tuple[str, str, str]] = {}
        self._completed_leases: dict[str, RunLease] = {}
        self._canonical_runs: dict[tuple[str, str, str], RunLease] = {}
        self._query_hashes: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def load(
        self,
        tenant_id: str,
        user_id: str,
        request_id: str,
        query_hash: str,
    ) -> AskResponse | None:
        key = (tenant_id, user_id, request_id)
        with self._lock:
            existing_hash = self._query_hashes.get(key)
            if existing_hash is not None and not hmac.compare_digest(existing_hash, query_hash):
                raise RequestIdConflict("The request ID was already used for another query.")
            return self._responses.get(key)

    def begin(self, start: RunStart) -> RunLease | None:
        key = (start.tenant_id, start.user_id, start.request_id)
        with self._lock:
            existing_hash = self._query_hashes.get(key)
            if existing_hash is not None and not hmac.compare_digest(
                existing_hash, start.query_hash
            ):
                raise RequestIdConflict("The request ID was already used for another query.")
            if key in self._responses or key in self._pending:
                return None
            previous = self._canonical_runs.get(key)
            lease = RunLease(
                run_id=previous.run_id if previous else start.run_id,
                generation=previous.generation + 1 if previous else 1,
            )
            self._pending[key] = lease
            self._run_keys[lease.run_id] = key
            self._canonical_runs[key] = lease
            self._query_hashes[key] = start.query_hash
            return lease

    def require_active(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> None:
        key = (tenant_id, user_id, request_id)
        with self._lock:
            if self._run_keys.get(lease.run_id) != key or self._pending.get(key) != lease:
                raise RunStoreUnavailable("Agent run lease is no longer owned.")

    def is_completed(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> bool:
        key = (tenant_id, user_id, request_id)
        with self._lock:
            response = self._responses.get(key)
            return bool(
                response
                and response.run_id == lease.run_id
                and self._completed_leases.get(lease.run_id) == lease
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
        key = (tenant_id, user_id, response.request_id)
        with self._lock:
            if response.run_id != lease.run_id or self._pending.get(key) != lease:
                raise RunStoreUnavailable("Agent run lease is no longer owned.")
            self._responses[key] = response
            self._completed_leases[lease.run_id] = lease
            self._pending.pop(key, None)
            self._run_keys.pop(lease.run_id, None)

    def fail(self, lease: RunLease, safe_error_code: str) -> None:
        with self._lock:
            key = self._run_keys.get(lease.run_id)
            if key is not None and self._pending.get(key) == lease:
                self._run_keys.pop(lease.run_id, None)
                self._pending.pop(key, None)

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
                    locale, correlation_id, lease_expires_at, lease_generation)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', %s, %s, %s, %s,
                        CURRENT_TIMESTAMP + INTERVAL '2 minutes', 1)
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
                ),
            ).fetchone()
            if row is not None:
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
                       lease_generation = lease_generation + 1
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
                    existing[0],
                ),
            ).fetchone()
            return (
                RunLease(run_id=str(retry[0]), generation=int(retry[1]))
                if retry is not None
                else None
            )

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
                       completed_at = %s, lease_expires_at = NULL
                 WHERE run_id = %s AND lease_generation = %s
                   AND run_state = 'RUNNING'
                   AND lease_expires_at > CURRENT_TIMESTAMP
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
                    UUID(lease.run_id),
                    lease.generation,
                ),
            ).rowcount
            if updated != 1:
                raise RunStoreUnavailable("Agent run could not be completed.")
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
            connection.execute(
                """
                UPDATE ai_agent_runs
                   SET run_state = 'FAILED', safe_error_code = %s,
                       completed_at = CURRENT_TIMESTAMP, lease_expires_at = NULL
                 WHERE run_id = %s AND run_state = 'RUNNING'
                   AND lease_generation = %s
                   AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (safe_error_code[:120], UUID(lease.run_id), lease.generation),
            )


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
