CREATE TABLE ai_conversations (
    conversation_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    locale VARCHAR(40) NOT NULL,
    title_nonce BYTEA NOT NULL,
    title_ciphertext BYTEA NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    retention_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_conversations_count CHECK (message_count >= 0)
);

CREATE INDEX idx_ai_conversations_owner_activity
    ON ai_conversations (tenant_id, user_id, last_message_at DESC);
CREATE INDEX idx_ai_conversations_retention
    ON ai_conversations (retention_until);

CREATE TABLE ai_conversation_messages (
    message_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL
        REFERENCES ai_conversations(conversation_id) ON DELETE CASCADE,
    request_id VARCHAR(128) NOT NULL,
    run_id UUID REFERENCES ai_agent_runs(run_id) ON DELETE SET NULL,
    role VARCHAR(16) NOT NULL,
    payload_nonce BYTEA NOT NULL,
    payload_ciphertext BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_ai_conversation_message_request_role
        UNIQUE (conversation_id, request_id, role),
    CONSTRAINT ck_ai_conversation_message_role CHECK (role IN ('USER', 'ASSISTANT'))
);

CREATE INDEX idx_ai_conversation_messages_order
    ON ai_conversation_messages (conversation_id, created_at, message_id);

CREATE TABLE ai_answer_feedback (
    feedback_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ai_agent_runs(run_id) ON DELETE CASCADE,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    rating VARCHAR(8) NOT NULL,
    reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    comment_nonce BYTEA,
    comment_ciphertext BYTEA,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_ai_answer_feedback_run_user UNIQUE (run_id, user_id),
    CONSTRAINT ck_ai_answer_feedback_rating CHECK (rating IN ('UP', 'DOWN')),
    CONSTRAINT ck_ai_answer_feedback_comment_pair CHECK (
        (comment_nonce IS NULL AND comment_ciphertext IS NULL)
        OR (comment_nonce IS NOT NULL AND comment_ciphertext IS NOT NULL))
);

CREATE INDEX idx_ai_answer_feedback_tenant_created
    ON ai_answer_feedback (tenant_id, created_at DESC);

COMMENT ON TABLE ai_conversations IS
    'User-owned DWAI-ON conversation metadata; titles are AES-256-GCM encrypted.';
COMMENT ON TABLE ai_conversation_messages IS
    'Encrypted user and assistant messages. No conversation content is stored in plaintext.';
COMMENT ON TABLE ai_answer_feedback IS
    'Per-user answer quality feedback; optional comments are encrypted.';
