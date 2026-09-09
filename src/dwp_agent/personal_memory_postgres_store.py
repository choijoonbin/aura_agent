from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
    advisory_lock,
    retention_deadline,
)
from .personal_domain_security import PersonalDomainIdentity
from .personal_memory_contracts import (
    AiSourceKey,
    AiSourcePreference,
    ChangeMemoryStateRequest,
    CreateMemoryRequest,
    DeleteMemoryRequest,
    ExplicitMemoryValue,
    MemoryPreferenceState,
    MemoryState,
    PersonalAiControls,
    PersonalMemory,
    RuntimeMemorySelection,
    UpdateMemoryPreferenceRequest,
    UpdateMemoryRuntimePreferenceRequest,
    UpdateAiSourcePreferenceRequest,
    UpdateMemoryRequest,
)
from .personal_memory_postgres_base import (
    PersonalMemoryPostgresBase,
    SOURCE_PERMISSIONS,
    _MEMORY_SELECT,
)
from .personal_memory_policy import require_safe_explicit_memory


T = TypeVar("T")


def _translated(function: Callable[..., T]) -> Callable[..., T]:
    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> T:
        try:
            return function(*args, **kwargs)
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable("Personal AI controls are unavailable.") from error

    return wrapped


class PostgresPersonalMemoryStore(PersonalMemoryPostgresBase):

    @_translated
    def controls(self, identity: PersonalDomainIdentity) -> PersonalAiControls:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT memory_state, runtime_application_state, revision, updated_at
                     FROM ai_user_memory_preferences
                    WHERE tenant_id = %s AND user_id = %s""",
                (identity.tenant_id, identity.user_id),
            ).fetchone()
            return self._controls(row, self._source_preferences(connection, identity))

    @_translated
    def update_controls(
        self,
        identity: PersonalDomainIdentity,
        request: UpdateMemoryPreferenceRequest,
    ) -> PersonalAiControls:
        proof = self._proof(identity, "PREFERENCE", None, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "PREFERENCE", proof)
            if replay is not None:
                return PersonalAiControls.model_validate(replay)
            advisory_lock(connection, "memory-preference", identity.tenant_id, identity.user_id)
            row = connection.execute(
                """SELECT memory_state, runtime_application_state, revision, updated_at
                     FROM ai_user_memory_preferences
                    WHERE tenant_id = %s AND user_id = %s FOR UPDATE""",
                (identity.tenant_id, identity.user_id),
            ).fetchone()
            source_preferences = self._source_preferences(connection, identity)
            current = self._controls(row, source_preferences)
            if current.revision != request.expected_revision:
                raise GovernedDomainConflict("The memory preference revision has changed.")
            revision = current.revision + 1
            updated = connection.execute(
                """INSERT INTO ai_user_memory_preferences (
                       tenant_id, user_id, memory_state, runtime_application_state,
                       revision, updated_by_user_id)
                   VALUES (%s, %s, %s, 'UNSET', %s, %s)
                   ON CONFLICT (tenant_id, user_id) DO UPDATE SET
                       memory_state = EXCLUDED.memory_state,
                       runtime_application_state = CASE
                           WHEN EXCLUDED.memory_state = 'DISABLED' THEN 'DISABLED'
                           ELSE ai_user_memory_preferences.runtime_application_state
                       END,
                       revision = EXCLUDED.revision,
                       updated_by_user_id = EXCLUDED.updated_by_user_id,
                       updated_at = CURRENT_TIMESTAMP
                   RETURNING memory_state, runtime_application_state, revision, updated_at""",
                (
                    identity.tenant_id,
                    identity.user_id,
                    request.memory_state.value,
                    revision,
                    identity.user_id,
                ),
            ).fetchone()
            result = self._controls(updated, source_preferences)
            self._record_command(connection, identity, None, "PREFERENCE", request, proof, result)
            self._event(
                connection,
                identity,
                request.command_id,
                "PREFERENCE",
                identity.user_id,
                "PREFERENCE_CHANGED",
                current.memory_state.value,
                result.memory_state.value,
                revision,
                proof,
                request.reason_code,
                request.change_reason,
            )
            return result

    @_translated
    def update_runtime_controls(
        self,
        identity: PersonalDomainIdentity,
        request: UpdateMemoryRuntimePreferenceRequest,
    ) -> PersonalAiControls:
        proof = self._proof(identity, "RUNTIME_PREFERENCE", None, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(
                connection, identity, request.command_id, "RUNTIME_PREFERENCE", proof
            )
            if replay is not None:
                return PersonalAiControls.model_validate(replay)
            advisory_lock(connection, "memory-preference", identity.tenant_id, identity.user_id)
            row = connection.execute(
                """SELECT memory_state, runtime_application_state, revision, updated_at
                     FROM ai_user_memory_preferences
                    WHERE tenant_id = %s AND user_id = %s FOR UPDATE""",
                (identity.tenant_id, identity.user_id),
            ).fetchone()
            current = self._controls(row, self._source_preferences(connection, identity))
            if current.revision != request.expected_revision:
                raise GovernedDomainConflict("The memory preference revision has changed.")
            if request.runtime_application_state == MemoryPreferenceState.ENABLED and not current.memory_enabled:
                raise GovernedDomainConflict(
                    "Explicit memory storage must be enabled before answer personalization."
                )
            revision = current.revision + 1
            updated = connection.execute(
                """UPDATE ai_user_memory_preferences
                      SET runtime_application_state = %s, revision = %s,
                          updated_by_user_id = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND user_id = %s
                    RETURNING memory_state, runtime_application_state, revision, updated_at""",
                (
                    request.runtime_application_state.value,
                    revision,
                    identity.user_id,
                    identity.tenant_id,
                    identity.user_id,
                ),
            ).fetchone()
            if updated is None:
                raise GovernedDomainConflict("Explicit memory storage must be configured first.")
            result = self._controls(updated, self._source_preferences(connection, identity))
            self._record_command(
                connection, identity, None, "RUNTIME_PREFERENCE", request, proof, result
            )
            self._event(
                connection,
                identity,
                request.command_id,
                "PREFERENCE",
                identity.user_id,
                "RUNTIME_APPLICATION_CHANGED",
                current.runtime_application_state.value,
                result.runtime_application_state.value,
                revision,
                proof,
                request.reason_code,
                request.change_reason,
            )
            return result

    @_translated
    def update_source_preference(
        self,
        identity: PersonalDomainIdentity,
        source_key: AiSourceKey,
        request: UpdateAiSourcePreferenceRequest,
    ) -> AiSourcePreference:
        source_key = AiSourceKey(source_key)
        proof = self._proof(identity, "SOURCE_PREFERENCE", None, request, source_key)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(
                connection, identity, request.command_id, "SOURCE_PREFERENCE", proof
            )
            if replay is not None:
                return AiSourcePreference.model_validate(replay)
            advisory_lock(
                connection, "ai-source-preference", identity.tenant_id,
                identity.user_id, source_key.value,
            )
            row = connection.execute(
                """SELECT enabled, revision, updated_at
                     FROM ai_user_ai_source_preferences
                    WHERE tenant_id = %s AND user_id = %s AND source_key = %s
                    FOR UPDATE""",
                (identity.tenant_id, identity.user_id, source_key.value),
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != request.expected_revision:
                raise GovernedDomainConflict("The AI source preference revision has changed.")
            permission = SOURCE_PERMISSIONS[source_key.value]
            available = permission in identity.permissions
            if request.enabled and not available:
                raise GovernedDomainConflict(
                    "An unavailable source cannot be enabled for personal AI context."
                )
            revision = current_revision + 1
            updated = connection.execute(
                """INSERT INTO ai_user_ai_source_preferences (
                       tenant_id, user_id, source_key, enabled, revision,
                       updated_by_user_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, user_id, source_key) DO UPDATE SET
                       enabled = EXCLUDED.enabled, revision = EXCLUDED.revision,
                       updated_by_user_id = EXCLUDED.updated_by_user_id,
                       updated_at = CURRENT_TIMESTAMP
                   RETURNING enabled, revision, updated_at""",
                (identity.tenant_id, identity.user_id, source_key.value,
                 request.enabled, revision, identity.user_id),
            ).fetchone()
            result = self._source_preference(source_key, updated, available)
            self._record_command(
                connection, identity, None, "SOURCE_PREFERENCE", request, proof, result
            )
            self._event(
                connection, identity, request.command_id, "SOURCE", source_key.value,
                "SOURCE_PREFERENCE_CHANGED", str(bool(row["enabled"])) if row else None,
                str(request.enabled), revision, proof, request.reason_code,
                request.change_reason,
            )
            return result

    @_translated
    def list(self, identity: PersonalDomainIdentity) -> list[PersonalMemory]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                _MEMORY_SELECT
                + " WHERE tenant_id = %s AND user_id = %s AND memory_state <> 'DELETED'"
                + " ORDER BY updated_at DESC",
                (identity.tenant_id, identity.user_id),
            ).fetchall()
            return [self._memory(row) for row in rows]

    @_translated
    def runtime_preferences(
        self, *, tenant_id: int, user_id: str
    ) -> RuntimeMemorySelection:
        with connect(self.database_url, row_factory=dict_row) as connection:
            preference = connection.execute(
                """SELECT memory_state, runtime_application_state
                     FROM ai_user_memory_preferences
                    WHERE tenant_id = %s AND user_id = %s""",
                (tenant_id, user_id),
            ).fetchone()
            storage_enabled = bool(
                preference and preference["memory_state"] == MemoryPreferenceState.ENABLED.value
            )
            runtime_enabled = bool(
                preference
                and preference["runtime_application_state"]
                == MemoryPreferenceState.ENABLED.value
            )
            if not storage_enabled or not runtime_enabled:
                return RuntimeMemorySelection(storage_enabled, runtime_enabled, ())
            rows = connection.execute(
                """SELECT DISTINCT ON (memory_kind)
                          memory_id, tenant_id, user_id, memory_kind, memory_state,
                          revision, payload_envelope, payload_fingerprint,
                          created_at, updated_at
                     FROM ai_user_memories
                    WHERE tenant_id = %s AND user_id = %s
                      AND memory_state = 'ACTIVE'
                      AND retention_until > CURRENT_TIMESTAMP
                    ORDER BY memory_kind, updated_at DESC, memory_id DESC""",
                (tenant_id, user_id),
            ).fetchall()
            return RuntimeMemorySelection(
                storage_enabled,
                runtime_enabled,
                tuple(self._memory(row) for row in rows),
            )

    @_translated
    def create(
        self, identity: PersonalDomainIdentity, request: CreateMemoryRequest
    ) -> PersonalMemory:
        require_safe_explicit_memory(request.memory)
        if request.expected_revision != 0:
            raise GovernedDomainConflict("A memory must start at revision zero.")
        proof = self._proof(identity, "CREATE", None, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "CREATE", proof)
            if replay is not None:
                return PersonalMemory.model_validate(replay)
            self._require_enabled(connection, identity)
            days = self._retention_days(connection, identity.tenant_id)
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            memory_id = uuid4()
            envelope, fingerprint = self._encoded_memory(identity, memory_id, request.memory)
            connection.execute(
                """INSERT INTO ai_user_memories (
                       memory_id, tenant_id, user_id, memory_kind, payload_envelope,
                       payload_fingerprint, retention_until, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    memory_id,
                    identity.tenant_id,
                    identity.user_id,
                    request.kind.value,
                    envelope,
                    fingerprint,
                    retention_deadline(now, days),
                    now,
                    now,
                ),
            )
            result = self._memory(self._locked(connection, identity, memory_id, lock=False))
            self._record_command(connection, identity, memory_id, "CREATE", request, proof, result)
            self._event(connection, identity, request.command_id, "MEMORY", str(memory_id), "CREATED", None, result.state.value, 1, proof, request.reason_code, None)
            return result

    @_translated
    def update(
        self,
        identity: PersonalDomainIdentity,
        memory_id: UUID,
        request: UpdateMemoryRequest,
    ) -> PersonalMemory:
        require_safe_explicit_memory(request.memory)
        proof = self._proof(identity, "UPDATE", memory_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "UPDATE", proof)
            if replay is not None:
                return PersonalMemory.model_validate(replay)
            self._require_enabled(connection, identity)
            row = self._locked(connection, identity, memory_id)
            self._expected_active(row, request.expected_revision)
            envelope, fingerprint = self._encoded_memory(identity, memory_id, request.memory)
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE ai_user_memories
                      SET payload_envelope = %s, payload_fingerprint = %s,
                          revision = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE memory_id = %s AND tenant_id = %s AND user_id = %s""",
                (envelope, fingerprint, revision, memory_id, identity.tenant_id, identity.user_id),
            )
            result = self._memory(self._locked(connection, identity, memory_id, lock=False))
            self._record_command(connection, identity, memory_id, "UPDATE", request, proof, result)
            self._event(connection, identity, request.command_id, "MEMORY", str(memory_id), "UPDATED", row["memory_state"], result.state.value, revision, proof, request.reason_code, None)
            return result

    @_translated
    def change_state(
        self,
        identity: PersonalDomainIdentity,
        memory_id: UUID,
        request: ChangeMemoryStateRequest,
    ) -> PersonalMemory:
        proof = self._proof(identity, "STATE", memory_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "STATE", proof)
            if replay is not None:
                return PersonalMemory.model_validate(replay)
            if request.memory_state == MemoryState.ACTIVE:
                self._require_enabled(connection, identity)
            row = self._locked(connection, identity, memory_id)
            self._expected_active(row, request.expected_revision)
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE ai_user_memories
                      SET memory_state = %s, revision = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE memory_id = %s AND tenant_id = %s AND user_id = %s""",
                (request.memory_state.value, revision, memory_id, identity.tenant_id, identity.user_id),
            )
            result = self._memory(self._locked(connection, identity, memory_id, lock=False))
            self._record_command(connection, identity, memory_id, "STATE", request, proof, result)
            self._event(connection, identity, request.command_id, "MEMORY", str(memory_id), "STATE_CHANGED", row["memory_state"], result.state.value, revision, proof, request.reason_code, request.change_reason)
            return result

    @_translated
    def delete(
        self,
        identity: PersonalDomainIdentity,
        memory_id: UUID,
        request: DeleteMemoryRequest,
    ) -> PersonalMemory:
        proof = self._proof(identity, "DELETE", memory_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "DELETE", proof)
            if replay is not None:
                return PersonalMemory.model_validate(replay)
            row = self._locked(connection, identity, memory_id)
            self._expected_active(row, request.expected_revision)
            tombstone = ExplicitMemoryValue(value="Deleted by explicit user privacy command")
            envelope, fingerprint = self._encoded_memory(identity, memory_id, tombstone)
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE ai_user_memories
                      SET memory_state = 'DELETED', payload_envelope = %s,
                          payload_fingerprint = %s, revision = %s,
                          deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE memory_id = %s AND tenant_id = %s AND user_id = %s""",
                (envelope, fingerprint, revision, memory_id, identity.tenant_id, identity.user_id),
            )
            result = self._memory(self._locked(connection, identity, memory_id, lock=False))
            self._record_command(connection, identity, memory_id, "DELETE", request, proof, result)
            self._event(connection, identity, request.command_id, "MEMORY", str(memory_id), "DELETED", row["memory_state"], MemoryState.DELETED.value, revision, proof, request.reason_code, request.change_reason)
            return result
