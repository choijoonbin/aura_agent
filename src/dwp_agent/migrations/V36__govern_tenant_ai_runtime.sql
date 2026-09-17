ALTER TABLE ai_governance_events
    DROP CONSTRAINT ck_ai_governance_event_category;

ALTER TABLE ai_governance_events
    ADD CONSTRAINT ck_ai_governance_event_category
        CHECK (category IN (
            'SOURCE', 'ACTION', 'SAFETY', 'EVALUATION', 'RETENTION', 'GATE', 'AI_CONTROL'
        ));

CREATE TABLE ai_execution_policies (
    tenant_id BIGINT PRIMARY KEY,
    emergency_disabled BOOLEAN NOT NULL DEFAULT FALSE,
    allowed_model_routes JSONB NOT NULL,
    allowed_tool_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    allowed_knowledge_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_output_tokens_per_request INTEGER NOT NULL DEFAULT 900,
    budget_enforcement_mode VARCHAR(20) NOT NULL DEFAULT 'ALERT_ONLY',
    period_token_limit BIGINT,
    alert_threshold_percent INTEGER NOT NULL DEFAULT 80,
    require_evaluation_pass BOOLEAN NOT NULL DEFAULT FALSE,
    evaluation_gate_status VARCHAR(24) NOT NULL DEFAULT 'NOT_REQUIRED',
    evaluation_observed_at TIMESTAMPTZ,
    evaluation_policy_version INTEGER,
    policy_version INTEGER NOT NULL DEFAULT 1,
    updated_by VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_execution_policy_routes CHECK (
        jsonb_typeof(allowed_model_routes) = 'array'
        AND jsonb_array_length(allowed_model_routes) BETWEEN 1 AND 50),
    CONSTRAINT ck_ai_execution_policy_tools CHECK (
        jsonb_typeof(allowed_tool_keys) = 'array'
        AND jsonb_array_length(allowed_tool_keys) <= 100),
    CONSTRAINT ck_ai_execution_policy_sources CHECK (
        jsonb_typeof(allowed_knowledge_sources) = 'array'
        AND jsonb_array_length(allowed_knowledge_sources) <= 50),
    CONSTRAINT ck_ai_execution_policy_output_limit CHECK (
        max_output_tokens_per_request BETWEEN 128 AND 4096),
    CONSTRAINT ck_ai_execution_policy_budget_mode CHECK (
        budget_enforcement_mode IN ('ALERT_ONLY', 'ENFORCED')),
    CONSTRAINT ck_ai_execution_policy_budget_limit CHECK (
        period_token_limit IS NULL OR period_token_limit BETWEEN 1 AND 10000000000),
    CONSTRAINT ck_ai_execution_policy_enforced_limit CHECK (
        budget_enforcement_mode <> 'ENFORCED' OR period_token_limit IS NOT NULL),
    CONSTRAINT ck_ai_execution_policy_alert CHECK (
        alert_threshold_percent BETWEEN 1 AND 100),
    CONSTRAINT ck_ai_execution_policy_evaluation CHECK (
        evaluation_gate_status IN ('NOT_REQUIRED', 'PENDING', 'PASSED', 'FAILED', 'STALE')
        AND (
            evaluation_gate_status <> 'PASSED'
            OR (evaluation_observed_at IS NOT NULL AND evaluation_policy_version IS NOT NULL)
        )),
    CONSTRAINT ck_ai_execution_policy_versions CHECK (
        policy_version >= 1
        AND (evaluation_policy_version IS NULL OR evaluation_policy_version >= 1))
);

CREATE TABLE ai_runtime_usage_periods (
    tenant_id BIGINT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    measured_input_tokens BIGINT NOT NULL DEFAULT 0,
    measured_output_tokens BIGINT NOT NULL DEFAULT 0,
    measured_total_tokens BIGINT NOT NULL DEFAULT 0,
    measurement_observed_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, period_start),
    CONSTRAINT ck_ai_runtime_usage_period CHECK (period_end > period_start),
    CONSTRAINT ck_ai_runtime_usage_nonnegative CHECK (
        measured_input_tokens >= 0
        AND measured_output_tokens >= 0
        AND measured_total_tokens >= 0)
);

CREATE TABLE ai_runtime_usage_reservations (
    reservation_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    run_id UUID NOT NULL,
    attempt_generation INTEGER NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    policy_version INTEGER NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uk_ai_runtime_usage_reservation_attempt
        UNIQUE (tenant_id, run_id, attempt_generation),
    CONSTRAINT uk_ai_runtime_usage_reservation_tenant_attempt
        UNIQUE (reservation_id, tenant_id, attempt_generation),
    CONSTRAINT ck_ai_runtime_usage_reservation_tokens CHECK (reserved_tokens > 0),
    CONSTRAINT ck_ai_runtime_usage_reservation_version CHECK (policy_version >= 1),
    CONSTRAINT ck_ai_runtime_usage_reservation_attempt CHECK (attempt_generation >= 1),
    CONSTRAINT ck_ai_runtime_usage_reservation_state CHECK (
        state IN ('ACTIVE', 'SETTLED', 'RELEASED', 'MEASUREMENT_MISSING')),
    CONSTRAINT ck_ai_runtime_usage_reservation_completion CHECK (
        (state = 'ACTIVE' AND completed_at IS NULL)
        OR (state <> 'ACTIVE' AND completed_at IS NOT NULL))
);

CREATE INDEX idx_ai_runtime_usage_reservation_active
    ON ai_runtime_usage_reservations (tenant_id, period_start, expires_at)
    WHERE state = 'ACTIVE';

CREATE INDEX idx_ai_runtime_usage_reservation_unmeasured
    ON ai_runtime_usage_reservations (tenant_id, period_start)
    WHERE state = 'MEASUREMENT_MISSING';

CREATE TABLE ai_runtime_usage_measurements (
    measurement_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    reservation_id UUID NOT NULL,
    run_id UUID NOT NULL,
    attempt_generation INTEGER NOT NULL,
    provider VARCHAR(40) NOT NULL,
    model VARCHAR(160) NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT fk_ai_runtime_usage_measurement_reservation
        FOREIGN KEY (reservation_id, tenant_id, attempt_generation)
        REFERENCES ai_runtime_usage_reservations(
            reservation_id, tenant_id, attempt_generation)
        ON DELETE RESTRICT,
    CONSTRAINT uk_ai_runtime_usage_measurement_attempt
        UNIQUE (tenant_id, run_id, attempt_generation),
    CONSTRAINT ck_ai_runtime_usage_measurement_attempt CHECK (attempt_generation >= 1),
    CONSTRAINT ck_ai_runtime_usage_measurement_tokens CHECK (
        input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0)
);

CREATE INDEX idx_ai_runtime_usage_measurement_tenant_observed
    ON ai_runtime_usage_measurements (tenant_id, observed_at DESC);

COMMENT ON TABLE ai_execution_policies IS
    'Tenant-scoped ASK_RUNTIME model, knowledge, evaluation, emergency stop, and token budget policy. Tool keys are declarative until a governed tool executor is connected. No credentials are stored.';
COMMENT ON TABLE ai_runtime_usage_reservations IS
    'Per-run-generation token reservations. Missing provider telemetry remains conservatively charged as MEASUREMENT_MISSING until reconciliation.';
COMMENT ON TABLE ai_runtime_usage_measurements IS
    'Locally observed model token use. It is not provider billing or price confirmation.';
