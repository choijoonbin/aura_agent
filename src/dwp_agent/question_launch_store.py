from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError
from psycopg import connect

from .envelope import EnvelopeEncryptionError, KeyContext, PayloadEncryption
from .run_store import load_payload_encryption
from .run_store_errors import RunStoreUnavailable


LOGGER = logging.getLogger(__name__)
LAUNCH_TTL_SECONDS = 60
MAX_ACTIVE_LAUNCHES = 8
MAX_CREATES_PER_MINUTE = 20


class QuestionLaunchUnavailable(RuntimeError):
    pass


class QuestionLaunchNotFound(RuntimeError):
    pass


class QuestionLaunchCapacityExceeded(RuntimeError):
    retry_after_seconds = LAUNCH_TTL_SECONDS


@dataclass(frozen=True)
class StoredQuestionLaunch:
    launch_id: UUID
    expires_at: datetime


class QuestionLaunchStore(Protocol):
    def create(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_family_id: str,
        question: str,
    ) -> StoredQuestionLaunch: ...

    def consume(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_family_id: str,
        launch_id: UUID,
    ) -> str: ...

    def delete_expired(self) -> tuple[int, int]: ...


class PostgresQuestionLaunchStore:
    def __init__(self, database_url: str, encryption: PayloadEncryption) -> None:
        self.database_url = database_url
        self.encryption = encryption

    def create(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_family_id: str,
        question: str,
    ) -> StoredQuestionLaunch:
        try:
            tenant = _tenant(tenant_id)
            launch_id = uuid4()
            envelope = self.encryption.encrypt_bytes(
                question.encode("utf-8"), _launch_context(tenant, launch_id)
            )
            with connect(self.database_url) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"dwp-agent:question-launch:{tenant}:{user_id}:{session_family_id}",),
                )
                now = connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                active_count, recent_count = connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM ai_question_launch_tickets
                         WHERE tenant_id = %s AND user_id = %s
                           AND session_family_id = %s AND expires_at > %s),
                       (SELECT COUNT(*) FROM ai_question_launch_rate_events
                         WHERE tenant_id = %s AND user_id = %s
                           AND session_family_id = %s
                           AND created_at > %s - INTERVAL '60 seconds')""",
                (
                    tenant,
                    user_id,
                    session_family_id,
                    now,
                    tenant,
                    user_id,
                    session_family_id,
                    now,
                ),
                ).fetchone()
                if active_count >= MAX_ACTIVE_LAUNCHES or recent_count >= MAX_CREATES_PER_MINUTE:
                    raise QuestionLaunchCapacityExceeded(
                        "Question launch capacity is temporarily exhausted."
                    )
                expires_at = now + timedelta(seconds=LAUNCH_TTL_SECONDS)
                connection.execute(
                """INSERT INTO ai_question_launch_tickets (
                       launch_id, tenant_id, user_id, session_family_id,
                       question_envelope, created_at, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    launch_id,
                    tenant,
                    user_id,
                    session_family_id,
                    envelope,
                    now,
                    expires_at,
                ),
                )
                connection.execute(
                """INSERT INTO ai_question_launch_rate_events (
                       tenant_id, user_id, session_family_id, created_at)
                   VALUES (%s, %s, %s, %s)""",
                (tenant, user_id, session_family_id, now),
                )
            return StoredQuestionLaunch(launch_id=launch_id, expires_at=expires_at)
        except QuestionLaunchCapacityExceeded:
            raise
        except (PsycopgError, EnvelopeEncryptionError, UnicodeEncodeError, ValueError) as error:
            raise QuestionLaunchUnavailable("Question launch storage is unavailable.") from error

    def consume(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_family_id: str,
        launch_id: UUID,
    ) -> str:
        tenant = _tenant(tenant_id)
        try:
            with connect(self.database_url) as connection:
                row = connection.execute(
                    """DELETE FROM ai_question_launch_tickets
                         WHERE launch_id = %s AND tenant_id = %s AND user_id = %s
                           AND session_family_id = %s AND expires_at > CURRENT_TIMESTAMP
                     RETURNING question_envelope""",
                    (launch_id, tenant, user_id, session_family_id),
                ).fetchone()
                if row is None:
                    raise QuestionLaunchNotFound("Question launch is unavailable.")
                envelope = str(row[0])
                connection.commit()
        except QuestionLaunchNotFound:
            raise
        except PsycopgError as error:
            raise QuestionLaunchUnavailable("Question launch storage is unavailable.") from error
        try:
            plaintext = self.encryption.decrypt_bytes(
                envelope=envelope,
                context=_launch_context(tenant, launch_id),
                legacy_version=None,
                legacy_nonce=None,
                legacy_ciphertext=None,
                legacy_aad=b"",
            )
            return plaintext.decode("utf-8")
        except (EnvelopeEncryptionError, UnicodeDecodeError, ValueError) as error:
            raise QuestionLaunchUnavailable("Question launch storage is unavailable.") from error

    def delete_expired(self) -> tuple[int, int]:
        try:
            with connect(self.database_url) as connection:
                ticket_result = connection.execute(
                    "DELETE FROM ai_question_launch_tickets WHERE expires_at <= CURRENT_TIMESTAMP"
                )
                rate_result = connection.execute(
                    """DELETE FROM ai_question_launch_rate_events
                         WHERE created_at <= CURRENT_TIMESTAMP - INTERVAL '5 minutes'"""
                )
            return ticket_result.rowcount, rate_result.rowcount
        except PsycopgError as error:
            raise QuestionLaunchUnavailable("Question launch storage is unavailable.") from error


def _tenant(value: str) -> int:
    try:
        tenant = int(value)
    except ValueError as error:
        raise QuestionLaunchUnavailable("Question launch storage is unavailable.") from error
    if tenant <= 0:
        raise QuestionLaunchUnavailable("Question launch storage is unavailable.")
    return tenant


def _launch_context(tenant_id: str | int, launch_id: UUID) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="question-launch",
        resource_id=str(launch_id),
        field="question",
    )


_STORE: QuestionLaunchStore | None = None
_STORE_LOCK = threading.Lock()


def get_question_launch_store() -> QuestionLaunchStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise QuestionLaunchUnavailable(
                "Question launch requires the configured Agent database."
            )
        try:
            _STORE = PostgresQuestionLaunchStore(database_url, load_payload_encryption())
        except RunStoreUnavailable as error:
            raise QuestionLaunchUnavailable(
                "Question launch storage is unavailable."
            ) from error
        return _STORE


def reset_question_launch_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


class QuestionLaunchMaintenance:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not os.getenv("DWP_AGENT_DATABASE_URL", "").strip() or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="dwp-agent-question-launch-maintenance",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        interval = _cleanup_interval_seconds()
        while not self._stop.wait(interval):
            try:
                get_question_launch_store().delete_expired()
            except Exception as error:
                LOGGER.warning(
                    "Question launch maintenance failed; error=%s",
                    type(error).__name__,
                )


def _cleanup_interval_seconds() -> int:
    raw = os.getenv("DWP_AGENT_QUESTION_LAUNCH_CLEANUP_SECONDS", "60").strip()
    try:
        configured = int(raw)
    except ValueError:
        configured = 60
    return max(10, min(300, configured))


MAINTENANCE = QuestionLaunchMaintenance()
