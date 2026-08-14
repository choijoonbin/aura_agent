from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import connect

from .contracts import AskResponse


class RunInProgress(RuntimeError):
    pass


class RequestIdConflict(RuntimeError):
    pass


class RunStoreUnavailable(RuntimeError):
    pass


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


class RunStore(Protocol):
    def load(
        self,
        tenant_id: str,
        user_id: str,
        request_id: str,
        query_hash: str,
    ) -> AskResponse | None: ...

    def begin(self, start: RunStart) -> bool: ...

    def complete(
        self,
        response: AskResponse,
        *,
        tenant_id: str,
        user_id: str,
        provider_request_hash: str | None = None,
    ) -> None: ...

    def fail(self, run_id: str, safe_error_code: str) -> None: ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._responses: dict[tuple[str, str, str], AskResponse] = {}
        self._pending: set[tuple[str, str, str]] = set()
        self._run_keys: dict[str, tuple[str, str, str]] = {}
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

    def begin(self, start: RunStart) -> bool:
        key = (start.tenant_id, start.user_id, start.request_id)
        with self._lock:
            existing_hash = self._query_hashes.get(key)
            if existing_hash is not None and not hmac.compare_digest(
                existing_hash, start.query_hash
            ):
                raise RequestIdConflict("The request ID was already used for another query.")
            if key in self._responses or key in self._pending:
                return False
            self._pending.add(key)
            self._run_keys[start.run_id] = key
            self._query_hashes[key] = start.query_hash
            return True

    def complete(
        self,
        response: AskResponse,
        *,
        tenant_id: str,
        user_id: str,
        provider_request_hash: str | None = None,
    ) -> None:
        key = (tenant_id, user_id, response.request_id)
        with self._lock:
            self._responses[key] = response
            self._pending.discard(key)

    def fail(self, run_id: str, safe_error_code: str) -> None:
        with self._lock:
            key = self._run_keys.pop(run_id, None)
            if key is not None:
                self._pending.discard(key)


class PayloadCipher:
    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except (ValueError, binascii.Error) as error:
            raise RunStoreUnavailable("Agent data key is not valid base64.") from error
        if len(key) != 32:
            raise RunStoreUnavailable("Agent data key must contain 32 bytes.")
        self._cipher = AESGCM(key)

    def encrypt(self, response: AskResponse, aad: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        payload = response.model_dump_json(by_alias=True).encode("utf-8")
        return nonce, self._cipher.encrypt(nonce, payload, aad)

    def decrypt(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> AskResponse:
        payload = self._cipher.decrypt(nonce, ciphertext, aad)
        return AskResponse.model_validate_json(payload)


class PostgresRunStore:
    def __init__(self, database_url: str, data_key: str) -> None:
        self.database_url = database_url
        self.cipher = PayloadCipher(data_key)

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
                SELECT run_id, response_nonce, response_ciphertext, run_state, query_hash
                  FROM ai_agent_runs
                 WHERE tenant_id = %s AND user_id = %s AND request_id = %s
                """,
                (int(tenant_id), user_id, request_id),
            ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(str(row[4]).strip(), query_hash):
            raise RequestIdConflict("The request ID was already used for another query.")
        if row[3] == "RUNNING":
            raise RunInProgress("An Ask request with this request ID is already running.")
        if row[1] is None or row[2] is None:
            return None
        return self.cipher.decrypt(
            bytes(row[1]),
            bytes(row[2]),
            _aad(tenant_id, user_id, request_id, str(row[0])),
        )

    def begin(self, start: RunStart) -> bool:
        with connect(self.database_url) as connection:
            row = connection.execute(
                """
                INSERT INTO ai_agent_runs (
                    run_id, tenant_id, user_id, request_id, query_hash,
                    agent_key, agent_revision, run_state, risk_tier, policy_outcome,
                    locale, correlation_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', %s, %s, %s, %s)
                ON CONFLICT (tenant_id, user_id, request_id) DO NOTHING
                RETURNING run_id
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
                return True

            existing = connection.execute(
                """
                SELECT run_id, query_hash, run_state
                  FROM ai_agent_runs
                 WHERE tenant_id = %s AND user_id = %s AND request_id = %s
                   FOR UPDATE
                """,
                (int(start.tenant_id), start.user_id, start.request_id),
            ).fetchone()
            if existing is None:
                return False
            if not hmac.compare_digest(str(existing[1]).strip(), start.query_hash):
                raise RequestIdConflict("The request ID was already used for another query.")
            if existing[2] != "FAILED":
                return False

            connection.execute(
                "DELETE FROM ai_agent_runs WHERE run_id = %s AND run_state = 'FAILED'",
                (existing[0],),
            )
            retry = connection.execute(
                """
                INSERT INTO ai_agent_runs (
                    run_id, tenant_id, user_id, request_id, query_hash,
                    agent_key, agent_revision, run_state, risk_tier, policy_outcome,
                    locale, correlation_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', %s, %s, %s, %s)
                RETURNING run_id
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
            return retry is not None

    def complete(
        self,
        response: AskResponse,
        *,
        tenant_id: str,
        user_id: str,
        provider_request_hash: str | None = None,
    ) -> None:
        nonce, ciphertext = self.cipher.encrypt(
            response,
            _aad(tenant_id, user_id, response.request_id, response.run_id),
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
                       response_nonce = %s,
                       response_ciphertext = %s,
                       completed_at = %s
                 WHERE run_id = %s AND run_state = 'RUNNING'
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
                    nonce,
                    ciphertext,
                    response.completed_at,
                    UUID(response.run_id),
                ),
            ).rowcount
            if updated != 1:
                raise RunStoreUnavailable("Agent run could not be completed.")
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

    def fail(self, run_id: str, safe_error_code: str) -> None:
        with connect(self.database_url) as connection:
            connection.execute(
                """
                UPDATE ai_agent_runs
                   SET run_state = 'FAILED', safe_error_code = %s,
                       completed_at = CURRENT_TIMESTAMP
                 WHERE run_id = %s AND run_state = 'RUNNING'
                """,
                (safe_error_code[:120], UUID(run_id)),
            )


_STORE: RunStore | None = None
_STORE_LOCK = threading.Lock()
_DATABASE_STATUS = "DISABLED"


def initialize_database() -> None:
    global _DATABASE_STATUS
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    required = os.getenv("DWP_AGENT_DATABASE_REQUIRED", "false").lower() == "true"
    if not database_url:
        _DATABASE_STATUS = "MISSING" if required else "DISABLED"
        if required:
            raise RunStoreUnavailable("Agent database is required but not configured.")
        return
    try:
        _apply_migrations(database_url)
        _DATABASE_STATUS = "READY"
    except Exception:
        _DATABASE_STATUS = "FAILED"
        raise


def database_status() -> str:
    return _DATABASE_STATUS


def get_run_store() -> RunStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if database_url:
            data_key = os.getenv("DWP_AGENT_DATA_KEY", "").strip()
            if not data_key:
                raise RunStoreUnavailable("Agent response encryption key is required.")
            _STORE = PostgresRunStore(database_url, data_key)
        else:
            _STORE = InMemoryRunStore()
        return _STORE


def reset_run_store_for_tests() -> None:
    global _STORE, _DATABASE_STATUS
    with _STORE_LOCK:
        _STORE = None
        _DATABASE_STATUS = "DISABLED"


def privacy_hash(value: str) -> str:
    secret = os.getenv("DWP_AGENT_PRIVACY_HASH_SECRET", "").strip()
    if not secret:
        secret = os.getenv("DWP_AGENT_DATA_KEY", "").strip()
    if not secret:
        raise RunStoreUnavailable("Agent privacy hash secret is required.")
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _apply_migrations(database_url: str) -> None:
    migration_dir = Path(__file__).resolve().parent / "migrations"
    migrations = sorted(migration_dir.glob("V*__*.sql"), key=lambda path: path.name)
    with connect(database_url) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sys_schema_history (
                version VARCHAR(40) PRIMARY KEY,
                description VARCHAR(200) NOT NULL,
                checksum CHAR(64) NOT NULL,
                installed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)
            """
        )
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (1_936_427_101,))
        applied = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT version, checksum FROM sys_schema_history"
            ).fetchall()
        }
        for migration in migrations:
            version, description = migration.stem.split("__", 1)
            sql = migration.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise RunStoreUnavailable(f"Agent migration checksum mismatch: {version}")
                continue
            connection.execute(sql)
            connection.execute(
                """
                INSERT INTO sys_schema_history (version, description, checksum)
                VALUES (%s, %s, %s)
                """,
                (version, description.replace("_", " "), checksum),
            )


def _aad(tenant_id: str, user_id: str, request_id: str, run_id: str) -> bytes:
    return f"{tenant_id}:{user_id}:{request_id}:{run_id}".encode("utf-8")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
