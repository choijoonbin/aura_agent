CREATE TABLE ai_retention_policy_events (
    event_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(128) NOT NULL,
    previous_retention_days INTEGER NOT NULL,
    retention_days INTEGER NOT NULL,
    previous_legal_hold BOOLEAN NOT NULL,
    legal_hold BOOLEAN NOT NULL,
    previous_policy_version INTEGER NOT NULL,
    policy_version INTEGER NOT NULL,
    change_reason VARCHAR(500) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_retention_policy_event_days
        CHECK (previous_retention_days BETWEEN 30 AND 3650
               AND retention_days BETWEEN 30 AND 3650),
    CONSTRAINT ck_ai_retention_policy_event_version
        CHECK (previous_policy_version >= 1
               AND policy_version = previous_policy_version + 1)
);

CREATE INDEX idx_ai_retention_policy_events_tenant_created
    ON ai_retention_policy_events (tenant_id, created_at DESC);

COMMENT ON TABLE ai_retention_policy_events IS
    'Append-only audit evidence for tenant DWAI-ON retention and legal-hold changes.';
