CREATE TABLE ai_conversation_retention_policies (
    tenant_id BIGINT PRIMARY KEY,
    retention_days INTEGER NOT NULL DEFAULT 90,
    legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
    policy_version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_conversation_retention_days
        CHECK (retention_days BETWEEN 30 AND 3650),
    CONSTRAINT ck_ai_conversation_retention_policy_version
        CHECK (policy_version >= 1)
);

INSERT INTO ai_conversation_retention_policies (tenant_id)
SELECT DISTINCT tenant_id FROM ai_conversations
ON CONFLICT (tenant_id) DO NOTHING;

ALTER TABLE ai_agent_runs
    ADD COLUMN response_key_version VARCHAR(64) NOT NULL DEFAULT 'legacy-v1';

ALTER TABLE ai_conversations
    ADD COLUMN encryption_key_version VARCHAR(64) NOT NULL DEFAULT 'legacy-v1';

ALTER TABLE ai_conversation_messages
    ADD COLUMN encryption_key_version VARCHAR(64) NOT NULL DEFAULT 'legacy-v1';

ALTER TABLE ai_answer_feedback
    ADD COLUMN encryption_key_version VARCHAR(64) NOT NULL DEFAULT 'legacy-v1';

COMMENT ON TABLE ai_conversation_retention_policies IS
    'Tenant-scoped DWAI-ON conversation retention and legal-hold policy.';
COMMENT ON COLUMN ai_conversation_retention_policies.legal_hold IS
    'When true, expiry cleanup and user deletion are blocked for all tenant conversations.';
COMMENT ON COLUMN ai_agent_runs.response_key_version IS
    'Application data-key version required to decrypt the idempotent response payload.';
COMMENT ON COLUMN ai_conversations.encryption_key_version IS
    'Application data-key version used for the encrypted conversation title.';
COMMENT ON COLUMN ai_conversation_messages.encryption_key_version IS
    'Application data-key version used for the encrypted message payload.';
