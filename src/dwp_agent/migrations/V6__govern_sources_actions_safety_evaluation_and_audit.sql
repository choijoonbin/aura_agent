CREATE TABLE ai_data_source_policies (
    tenant_id BIGINT NOT NULL,
    source_key VARCHAR(64) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    description VARCHAR(500) NOT NULL,
    provider_type VARCHAR(40) NOT NULL,
    classification VARCHAR(24) NOT NULL,
    access_mode VARCHAR(24) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    connection_state VARCHAR(32) NOT NULL DEFAULT 'NOT_CONFIGURED',
    connector_ref VARCHAR(160),
    policy_version INTEGER NOT NULL DEFAULT 1,
    updated_by VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, source_key),
    CONSTRAINT ck_ai_source_policy_classification
        CHECK (classification IN ('INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),
    CONSTRAINT ck_ai_source_policy_access_mode
        CHECK (access_mode IN ('SOURCE_PERMISSIONS', 'TENANT_ALLOWLIST', 'BLOCKED')),
    CONSTRAINT ck_ai_source_policy_connection_state
        CHECK (connection_state IN ('CONNECTED', 'DEGRADED', 'NOT_CONFIGURED', 'BLOCKED')),
    CONSTRAINT ck_ai_source_policy_version CHECK (policy_version >= 1),
    CONSTRAINT ck_ai_source_connector_ref CHECK (
        connector_ref IS NULL OR length(btrim(connector_ref)) BETWEEN 3 AND 160)
);

CREATE TABLE ai_action_policies (
    tenant_id BIGINT NOT NULL,
    action_key VARCHAR(128) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    confirmation_required BOOLEAN NOT NULL DEFAULT TRUE,
    execution_policy VARCHAR(32) NOT NULL DEFAULT 'USER_HANDOFF',
    policy_version INTEGER NOT NULL DEFAULT 1,
    updated_by VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, action_key),
    CONSTRAINT ck_ai_action_execution_policy
        CHECK (execution_policy IN ('USER_HANDOFF', 'APPROVAL_HANDOFF', 'BLOCKED')),
    CONSTRAINT ck_ai_action_policy_version CHECK (policy_version >= 1)
);

CREATE TABLE ai_safety_policies (
    tenant_id BIGINT PRIMARY KEY,
    prompt_injection_outcome VARCHAR(16) NOT NULL DEFAULT 'DENY',
    privileged_data_outcome VARCHAR(16) NOT NULL DEFAULT 'HANDOFF',
    mutation_outcome VARCHAR(16) NOT NULL DEFAULT 'HANDOFF',
    require_citations BOOLEAN NOT NULL DEFAULT TRUE,
    public_web_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    max_source_scopes INTEGER NOT NULL DEFAULT 4,
    max_tool_calls INTEGER NOT NULL DEFAULT 3,
    policy_version INTEGER NOT NULL DEFAULT 1,
    updated_by VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_safety_outcomes CHECK (
        prompt_injection_outcome IN ('DENY')
        AND privileged_data_outcome IN ('DENY', 'HANDOFF')
        AND mutation_outcome IN ('DENY', 'HANDOFF')),
    CONSTRAINT ck_ai_safety_source_scopes CHECK (max_source_scopes BETWEEN 1 AND 7),
    CONSTRAINT ck_ai_safety_tool_calls CHECK (max_tool_calls BETWEEN 0 AND 10),
    CONSTRAINT ck_ai_safety_policy_version CHECK (policy_version >= 1),
    CONSTRAINT ck_ai_safety_public_web_default_deny CHECK (public_web_enabled = FALSE)
);

CREATE TABLE ai_evaluation_sets (
    evaluation_set_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    name VARCHAR(160) NOT NULL,
    description VARCHAR(1000),
    locale VARCHAR(16) NOT NULL DEFAULT 'ko-KR',
    lifecycle_state VARCHAR(16) NOT NULL DEFAULT 'DRAFT',
    version INTEGER NOT NULL DEFAULT 1,
    created_by VARCHAR(160) NOT NULL,
    updated_by VARCHAR(160) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_evaluation_set_state
        CHECK (lifecycle_state IN ('DRAFT', 'ACTIVE', 'RETIRED')),
    CONSTRAINT ck_ai_evaluation_set_version CHECK (version >= 1)
);

CREATE INDEX idx_ai_evaluation_sets_tenant_updated
    ON ai_evaluation_sets (tenant_id, updated_at DESC);

CREATE TABLE ai_evaluation_cases (
    evaluation_case_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    evaluation_set_id UUID NOT NULL
        REFERENCES ai_evaluation_sets(evaluation_set_id) ON DELETE CASCADE,
    name VARCHAR(160) NOT NULL,
    prompt_nonce BYTEA NOT NULL,
    prompt_ciphertext BYTEA NOT NULL,
    expected_terms_nonce BYTEA NOT NULL,
    expected_terms_ciphertext BYTEA NOT NULL,
    encryption_key_version VARCHAR(64) NOT NULL,
    source_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    version INTEGER NOT NULL DEFAULT 1,
    created_by VARCHAR(160) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_ai_evaluation_case_name UNIQUE (tenant_id, evaluation_set_id, name),
    CONSTRAINT ck_ai_evaluation_case_version CHECK (version >= 1),
    CONSTRAINT ck_ai_evaluation_case_source_scopes CHECK (
        jsonb_typeof(source_scopes) = 'array' AND jsonb_array_length(source_scopes) <= 7)
);

CREATE TABLE ai_evaluation_runs (
    evaluation_run_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    evaluation_set_id UUID NOT NULL
        REFERENCES ai_evaluation_sets(evaluation_set_id) ON DELETE RESTRICT,
    run_state VARCHAR(32) NOT NULL,
    case_count INTEGER NOT NULL DEFAULT 0,
    passed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    configuration_required_count INTEGER NOT NULL DEFAULT 0,
    model_ref VARCHAR(160),
    created_by VARCHAR(160) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT ck_ai_evaluation_run_state CHECK (
        run_state IN ('RUNNING', 'COMPLETED', 'CONFIGURATION_REQUIRED', 'FAILED')),
    CONSTRAINT ck_ai_evaluation_run_counts CHECK (
        case_count >= 0 AND passed_count >= 0 AND failed_count >= 0
        AND configuration_required_count >= 0
        AND passed_count + failed_count + configuration_required_count <= case_count)
);

CREATE INDEX idx_ai_evaluation_runs_tenant_created
    ON ai_evaluation_runs (tenant_id, created_at DESC);

CREATE TABLE ai_evaluation_results (
    evaluation_result_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    evaluation_run_id UUID NOT NULL
        REFERENCES ai_evaluation_runs(evaluation_run_id) ON DELETE CASCADE,
    evaluation_case_id UUID NOT NULL
        REFERENCES ai_evaluation_cases(evaluation_case_id) ON DELETE RESTRICT,
    outcome VARCHAR(32) NOT NULL,
    status_code VARCHAR(80) NOT NULL,
    grounded BOOLEAN NOT NULL DEFAULT FALSE,
    expected_terms_matched INTEGER NOT NULL DEFAULT 0,
    expected_terms_total INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_ai_evaluation_result_case UNIQUE (evaluation_run_id, evaluation_case_id),
    CONSTRAINT ck_ai_evaluation_result_outcome CHECK (
        outcome IN ('PASS', 'FAIL', 'CONFIGURATION_REQUIRED')),
    CONSTRAINT ck_ai_evaluation_result_metrics CHECK (
        expected_terms_matched >= 0 AND expected_terms_total >= 0
        AND expected_terms_matched <= expected_terms_total AND latency_ms >= 0)
);

CREATE TABLE ai_governance_events (
    event_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    category VARCHAR(32) NOT NULL,
    event_type VARCHAR(96) NOT NULL,
    target_type VARCHAR(64) NOT NULL,
    target_key VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(128) NOT NULL,
    change_reason VARCHAR(500),
    previous_value JSONB,
    current_value JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_governance_event_category CHECK (
        category IN ('SOURCE', 'ACTION', 'SAFETY', 'EVALUATION', 'RETENTION'))
);

CREATE INDEX idx_ai_governance_events_tenant_created
    ON ai_governance_events (tenant_id, created_at DESC);
CREATE INDEX idx_ai_governance_events_tenant_category
    ON ai_governance_events (tenant_id, category, created_at DESC);

CREATE OR REPLACE FUNCTION reject_ai_audit_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'DWAI-ON audit evidence is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ai_governance_events_append_only
    BEFORE UPDATE OR DELETE ON ai_governance_events
    FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TRIGGER trg_ai_retention_policy_events_append_only
    BEFORE UPDATE OR DELETE ON ai_retention_policy_events
    FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_data_source_policies IS
    'Tenant source allowlist. Credentials are never stored; connector_ref points to an external secret-owning integration.';
COMMENT ON TABLE ai_action_policies IS
    'Tenant action allowlist applied after verified user permission checks.';
COMMENT ON TABLE ai_safety_policies IS
    'Versioned tenant safety boundary with public web access structurally disabled.';
COMMENT ON TABLE ai_evaluation_cases IS
    'Repeatable evaluation prompts and expected terms encrypted with the Agent data keyring.';
COMMENT ON TABLE ai_governance_events IS
    'Append-only, content-free governance evidence for DWAI-ON administration.';
