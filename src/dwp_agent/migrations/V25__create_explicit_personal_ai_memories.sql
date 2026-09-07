CREATE TABLE ai_user_memory_preferences (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    memory_state VARCHAR(16) NOT NULL,
    revision INTEGER NOT NULL,
    updated_by_user_id VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id),
    CONSTRAINT ck_ai_user_memory_preference_state CHECK (
        memory_state IN ('DISABLED', 'ENABLED')
    ),
    CONSTRAINT ck_ai_user_memory_preference_revision CHECK (revision > 0)
);

CREATE TABLE ai_user_memories (
    memory_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    memory_kind VARCHAR(32) NOT NULL,
    memory_state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
    revision INTEGER NOT NULL DEFAULT 1,
    payload_envelope TEXT NOT NULL,
    payload_fingerprint CHAR(64) NOT NULL,
    retention_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMPTZ,
    CONSTRAINT uq_ai_user_memory_owner UNIQUE (memory_id, tenant_id, user_id),
    CONSTRAINT ck_ai_user_memory_kind CHECK (
        memory_kind IN ('RESPONSE_LENGTH', 'OUTPUT_FORMAT', 'TONE', 'WORKING_STYLE')
    ),
    CONSTRAINT ck_ai_user_memory_state CHECK (
        memory_state IN ('ACTIVE', 'DISABLED', 'DELETED')
    ),
    CONSTRAINT ck_ai_user_memory_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_user_memory_payload CHECK (payload_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_user_memory_fingerprint CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_user_memory_deleted CHECK (
        (memory_state = 'DELETED' AND deleted_at IS NOT NULL)
        OR (memory_state <> 'DELETED' AND deleted_at IS NULL)
    )
);

CREATE INDEX idx_ai_user_memories_owner
    ON ai_user_memories (tenant_id, user_id, updated_at DESC)
    WHERE memory_state <> 'DELETED';

CREATE TABLE ai_user_memory_commands (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    memory_id UUID,
    command_type VARCHAR(24) NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    result_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_user_memory_command_type CHECK (
        command_type IN ('PREFERENCE', 'CREATE', 'UPDATE', 'STATE', 'DELETE')
    ),
    CONSTRAINT ck_ai_user_memory_command_session CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_user_memory_command_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_user_memory_command_result CHECK (result_envelope LIKE 'dwp2.%')
);

CREATE TRIGGER trg_ai_user_memory_commands_append_only
BEFORE UPDATE OR DELETE ON ai_user_memory_commands
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_user_memory_events (
    event_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    target_type VARCHAR(16) NOT NULL,
    target_id VARCHAR(160) NOT NULL,
    event_type VARCHAR(24) NOT NULL,
    previous_state VARCHAR(16),
    current_state VARCHAR(16) NOT NULL,
    revision INTEGER NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    change_reason_envelope TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_user_memory_event_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_user_memory_event_target CHECK (target_type IN ('PREFERENCE', 'MEMORY')),
    CONSTRAINT ck_ai_user_memory_event_type CHECK (
        event_type IN ('PREFERENCE_CHANGED', 'CREATED', 'UPDATED', 'STATE_CHANGED', 'DELETED')
    ),
    CONSTRAINT ck_ai_user_memory_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_user_memory_event_session CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_user_memory_event_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_user_memory_event_reason CHECK (reason_code ~ '^[A-Z][A-Z0-9_.-]{1,63}$'),
    CONSTRAINT ck_ai_user_memory_event_reason_envelope CHECK (
        change_reason_envelope IS NULL OR change_reason_envelope LIKE 'dwp2.%'
    )
);

CREATE TRIGGER trg_ai_user_memory_events_append_only
BEFORE UPDATE OR DELETE ON ai_user_memory_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_user_memories IS
    'Explicit user-authored AI preferences encrypted at rest; inferred sensitive memories are forbidden.';
