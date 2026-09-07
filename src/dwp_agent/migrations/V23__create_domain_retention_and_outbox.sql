CREATE TABLE ai_domain_retention_policies (
    tenant_id BIGINT NOT NULL,
    domain_key VARCHAR(32) NOT NULL,
    retention_days INTEGER NOT NULL,
    deletion_grace_days INTEGER NOT NULL DEFAULT 7,
    legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
    revision INTEGER NOT NULL,
    updated_by_user_id VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, domain_key),
    CONSTRAINT ck_ai_domain_retention_domain CHECK (
        domain_key IN ('ROUTINE', 'MEMORY', 'ARTIFACT', 'ARTIFACT_EXPORT')
    ),
    CONSTRAINT ck_ai_domain_retention_days CHECK (retention_days BETWEEN 1 AND 3650),
    CONSTRAINT ck_ai_domain_retention_grace CHECK (deletion_grace_days BETWEEN 0 AND 90),
    CONSTRAINT ck_ai_domain_retention_revision CHECK (revision > 0)
);

CREATE TABLE ai_domain_retention_events (
    event_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    domain_key VARCHAR(32) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    event_type VARCHAR(24) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    change_reason_envelope TEXT NOT NULL,
    previous_value JSONB,
    current_value JSONB NOT NULL,
    revision INTEGER NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_domain_retention_command UNIQUE (tenant_id, actor_user_id, command_id),
    CONSTRAINT ck_ai_domain_retention_event_domain CHECK (
        domain_key IN ('ROUTINE', 'MEMORY', 'ARTIFACT', 'ARTIFACT_EXPORT')
    ),
    CONSTRAINT ck_ai_domain_retention_event_type CHECK (
        event_type IN ('BOOTSTRAPPED', 'UPDATED')
    ),
    CONSTRAINT ck_ai_domain_retention_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_domain_retention_session CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_domain_retention_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_domain_retention_reason CHECK (reason_code ~ '^[A-Z][A-Z0-9_.-]{1,63}$'),
    CONSTRAINT ck_ai_domain_retention_reason_envelope CHECK (
        change_reason_envelope LIKE 'dwp2.%'
    )
);

CREATE TRIGGER trg_ai_domain_retention_events_append_only
BEFORE UPDATE OR DELETE ON ai_domain_retention_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_data_deletion_jobs (
    deletion_job_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    reason_code VARCHAR(64) NOT NULL,
    change_reason_envelope TEXT NOT NULL,
    result_envelope TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    retention_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_data_deletion_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_data_deletion_state CHECK (
        state IN ('REQUESTED', 'RUNNING', 'PARTIAL', 'COMPLETED',
                  'BLOCKED_LEGAL_HOLD', 'FAILED')
    ),
    CONSTRAINT ck_ai_data_deletion_generation CHECK (generation >= 0 AND attempt_count >= 0),
    CONSTRAINT ck_ai_data_deletion_session CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_data_deletion_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_data_deletion_reason CHECK (reason_code ~ '^[A-Z][A-Z0-9_.-]{1,63}$'),
    CONSTRAINT ck_ai_data_deletion_reason_envelope CHECK (change_reason_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_data_deletion_result_envelope CHECK (
        result_envelope IS NULL OR result_envelope LIKE 'dwp2.%'
    ),
    CONSTRAINT ck_ai_data_deletion_lease CHECK (
        (state = 'RUNNING' AND generation > 0 AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL)
        OR (state <> 'RUNNING' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_ai_data_deletion_completion CHECK (
        (state IN ('PARTIAL', 'COMPLETED', 'BLOCKED_LEGAL_HOLD', 'FAILED')
            AND completed_at IS NOT NULL)
        OR (state IN ('REQUESTED', 'RUNNING') AND completed_at IS NULL)
    )
);

CREATE INDEX idx_ai_data_deletion_jobs_owner
    ON ai_data_deletion_jobs (tenant_id, user_id, requested_at DESC);
CREATE INDEX idx_ai_data_deletion_jobs_claim
    ON ai_data_deletion_jobs (state, requested_at)
    WHERE state IN ('REQUESTED', 'RUNNING');

CREATE TABLE ai_data_deletion_targets (
    deletion_job_id UUID NOT NULL REFERENCES ai_data_deletion_jobs(deletion_job_id) ON DELETE RESTRICT,
    domain_key VARCHAR(32) NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'REQUESTED',
    affected_count BIGINT,
    safe_error_code VARCHAR(128),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (deletion_job_id, domain_key),
    CONSTRAINT ck_ai_data_deletion_target_domain CHECK (
        domain_key IN ('ROUTINE', 'MEMORY', 'ARTIFACT', 'ARTIFACT_EXPORT')
    ),
    CONSTRAINT ck_ai_data_deletion_target_state CHECK (
        state IN ('REQUESTED', 'RUNNING', 'COMPLETED', 'BLOCKED_LEGAL_HOLD', 'FAILED')
    ),
    CONSTRAINT ck_ai_data_deletion_target_count CHECK (
        affected_count IS NULL OR affected_count >= 0
    )
);

CREATE TABLE ai_data_deletion_events (
    event_id UUID PRIMARY KEY,
    deletion_job_id UUID NOT NULL REFERENCES ai_data_deletion_jobs(deletion_job_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    previous_state VARCHAR(32),
    current_state VARCHAR(32) NOT NULL,
    generation BIGINT NOT NULL,
    safe_error_code VARCHAR(128),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER trg_ai_data_deletion_events_append_only
BEFORE UPDATE OR DELETE ON ai_data_deletion_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_transactional_outbox (
    outbox_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    topic VARCHAR(96) NOT NULL,
    aggregate_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(160) NOT NULL,
    event_key CHAR(64) NOT NULL,
    payload_envelope TEXT NOT NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    generation BIGINT NOT NULL DEFAULT 0,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMPTZ,
    retention_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_transactional_outbox_event UNIQUE (tenant_id, topic, event_key),
    CONSTRAINT ck_ai_transactional_outbox_event_key CHECK (event_key ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_transactional_outbox_payload CHECK (payload_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_transactional_outbox_state CHECK (
        state IN ('PENDING', 'CLAIMED', 'DELIVERED', 'DEAD_LETTER', 'CANCELLED')
    ),
    CONSTRAINT ck_ai_transactional_outbox_counts CHECK (generation >= 0 AND attempt_count >= 0),
    CONSTRAINT ck_ai_transactional_outbox_lease CHECK (
        (state = 'CLAIMED' AND generation > 0 AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND delivered_at IS NULL)
        OR (state <> 'CLAIMED' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_ai_transactional_outbox_delivery CHECK (
        (state = 'DELIVERED' AND delivered_at IS NOT NULL)
        OR (state <> 'DELIVERED' AND delivered_at IS NULL)
    )
);

CREATE INDEX idx_ai_transactional_outbox_claim
    ON ai_transactional_outbox (state, available_at, created_at)
    WHERE state IN ('PENDING', 'CLAIMED');

COMMENT ON TABLE ai_domain_retention_policies IS
    'Explicit tenant retention and legal-hold policy for new DWAI-ON data domains.';
COMMENT ON TABLE ai_data_deletion_jobs IS
    'User-scoped deletion requests. REQUESTED is not evidence that data was deleted.';
COMMENT ON TABLE ai_transactional_outbox IS
    'Encrypted internal delivery intents. No row is evidence of an external side effect.';
