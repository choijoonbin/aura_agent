ALTER TABLE ai_user_memories
    ADD COLUMN application_scope TEXT[] NOT NULL DEFAULT ARRAY['ASK']::TEXT[],
    ADD COLUMN expires_at TIMESTAMPTZ,
    ADD COLUMN memory_origin VARCHAR(16) NOT NULL DEFAULT 'MANUAL',
    ADD COLUMN use_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN last_used_at TIMESTAMPTZ;

ALTER TABLE ai_user_memories
    ADD CONSTRAINT ck_ai_user_memory_scope_nonempty
        CHECK (cardinality(application_scope) > 0),
    ADD CONSTRAINT ck_ai_user_memory_scope_values
        CHECK (
            application_scope <@ ARRAY[
                'ASK', 'RESEARCH', 'PROPOSALS', 'ROUTINES', 'ARTIFACTS'
            ]::TEXT[]
        ),
    ADD CONSTRAINT ck_ai_user_memory_expiry
        CHECK (expires_at IS NULL OR expires_at <= retention_until),
    ADD CONSTRAINT ck_ai_user_memory_origin CHECK (memory_origin = 'MANUAL'),
    ADD CONSTRAINT ck_ai_user_memory_use_count CHECK (use_count >= 0);

CREATE INDEX idx_ai_user_memories_runtime_scope
    ON ai_user_memories (tenant_id, user_id, expires_at)
    WHERE memory_state = 'ACTIVE';

COMMENT ON COLUMN ai_user_memories.application_scope IS
    'Explicit governed product contexts in which this memory may be applied.';
COMMENT ON COLUMN ai_user_memories.expires_at IS
    'Optional user-selected expiry bounded by the tenant retention deadline.';
