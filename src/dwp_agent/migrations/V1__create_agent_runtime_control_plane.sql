CREATE TABLE ai_agent_runs (
    run_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    query_hash CHAR(64) NOT NULL,
    agent_key VARCHAR(100) NOT NULL,
    agent_revision INTEGER NOT NULL,
    run_state VARCHAR(32) NOT NULL,
    answer_state VARCHAR(32),
    risk_tier VARCHAR(8) NOT NULL,
    policy_outcome VARCHAR(24) NOT NULL,
    status_code VARCHAR(128),
    locale VARCHAR(40) NOT NULL,
    provider VARCHAR(80),
    model VARCHAR(160),
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms BIGINT NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    safe_error_code VARCHAR(128),
    correlation_id VARCHAR(128) NOT NULL,
    response_nonce BYTEA,
    response_ciphertext BYTEA,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uk_ai_agent_runs_request UNIQUE (tenant_id, user_id, request_id),
    CONSTRAINT ck_ai_agent_runs_state
        CHECK (run_state IN ('RUNNING', 'COMPLETED', 'FAILED')),
    CONSTRAINT ck_ai_agent_runs_answer_state
        CHECK (answer_state IS NULL OR answer_state IN (
            'COMPLETED', 'ABSTAINED', 'CONFIGURATION_REQUIRED')),
    CONSTRAINT ck_ai_agent_runs_risk CHECK (risk_tier IN ('L0', 'L1', 'L2', 'L3')),
    CONSTRAINT ck_ai_agent_runs_policy CHECK (policy_outcome IN ('ALLOW', 'HANDOFF', 'DENY')),
    CONSTRAINT ck_ai_agent_runs_revision CHECK (agent_revision >= 0),
    CONSTRAINT ck_ai_agent_runs_usage CHECK (
        input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0
        AND latency_ms >= 0 AND source_count >= 0)
);

CREATE INDEX idx_ai_agent_runs_tenant_created
    ON ai_agent_runs (tenant_id, created_at DESC);
CREATE INDEX idx_ai_agent_runs_state_created
    ON ai_agent_runs (run_state, created_at DESC);
CREATE INDEX idx_ai_agent_runs_correlation
    ON ai_agent_runs (tenant_id, correlation_id);

CREATE TABLE ai_model_calls (
    model_call_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ai_agent_runs(run_id) ON DELETE CASCADE,
    provider VARCHAR(80),
    model VARCHAR(160),
    call_state VARCHAR(32) NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms BIGINT NOT NULL DEFAULT 0,
    provider_request_hash CHAR(64),
    safe_error_code VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_model_calls_state CHECK (call_state IN (
        'COMPLETED', 'CONFIGURATION_REQUIRED', 'REFUSED')),
    CONSTRAINT ck_ai_model_calls_usage CHECK (
        input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0 AND latency_ms >= 0)
);

CREATE INDEX idx_ai_model_calls_run ON ai_model_calls (run_id, created_at);
CREATE INDEX idx_ai_model_calls_provider_created
    ON ai_model_calls (provider, model, created_at DESC);

CREATE TABLE ai_agent_citations (
    citation_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ai_agent_runs(run_id) ON DELETE CASCADE,
    source_ref_hash CHAR(64) NOT NULL,
    source_type VARCHAR(40) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_ai_agent_citations_run_source UNIQUE (run_id, source_ref_hash),
    CONSTRAINT ck_ai_agent_citations_type CHECK (
        source_type IN ('WORK_ITEM', 'MAIL', 'CALENDAR'))
);

COMMENT ON TABLE ai_agent_runs IS
    'Privacy-minimized Agent executions; query and answer payloads are never stored in plaintext.';
COMMENT ON COLUMN ai_agent_runs.query_hash IS
    'Keyed HMAC used for correlation without retaining the raw user question.';
COMMENT ON COLUMN ai_agent_runs.response_ciphertext IS
    'AES-256-GCM encrypted response used only for idempotent retry.';
COMMENT ON TABLE ai_agent_citations IS
    'Hashed citation references only; source titles and source content are intentionally excluded.';
