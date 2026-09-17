ALTER TABLE ai_personal_routine_advanced_commands
    ADD CONSTRAINT uq_ai_routine_advanced_effect_owner
        UNIQUE (command_id, routine_id, tenant_id, user_id);

ALTER TABLE ai_personal_routine_executions
    ADD CONSTRAINT uq_ai_routine_execution_effect_owner
        UNIQUE (routine_run_id, routine_id, tenant_id, user_id);

CREATE TABLE ai_personal_routine_engine_override_events (
    event_id UUID PRIMARY KEY,
    command_id UUID NOT NULL,
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    state_version INTEGER NOT NULL CHECK (state_version >= 1),
    action VARCHAR(16) NOT NULL CHECK (action IN ('APPLY', 'ROLLBACK')),
    agent_id VARCHAR(160),
    engine_id VARCHAR(160),
    expires_at TIMESTAMPTZ,
    provider_receipt_id VARCHAR(240) NOT NULL,
    result_sha256 CHAR(64) NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_ref VARCHAR(240) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_routine_engine_override_command
        FOREIGN KEY (command_id, routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routine_advanced_commands
            (command_id, routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_routine_engine_override_command UNIQUE (command_id),
    CONSTRAINT uq_ai_routine_engine_override_version
        UNIQUE (tenant_id, user_id, routine_id, state_version),
    CONSTRAINT ck_ai_routine_engine_override_payload CHECK (
        (action = 'APPLY' AND agent_id IS NOT NULL AND engine_id IS NOT NULL
            AND expires_at IS NOT NULL AND expires_at > occurred_at)
        OR (action = 'ROLLBACK' AND agent_id IS NULL AND engine_id IS NULL
            AND expires_at IS NULL)
    )
);

CREATE INDEX idx_ai_routine_engine_override_current
    ON ai_personal_routine_engine_override_events
        (tenant_id, user_id, routine_id, state_version DESC);

CREATE TRIGGER trg_ai_routine_engine_override_events_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_engine_override_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_personal_routine_budget_exceptions (
    command_id UUID PRIMARY KEY,
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    additional_runs INTEGER NOT NULL CHECK (additional_runs BETWEEN 0 AND 744),
    additional_tokens_per_run INTEGER NOT NULL
        CHECK (additional_tokens_per_run BETWEEN 0 AND 2000000),
    additional_minutes_per_run INTEGER NOT NULL
        CHECK (additional_minutes_per_run BETWEEN 0 AND 240),
    expires_at TIMESTAMPTZ NOT NULL,
    provider_receipt_id VARCHAR(240) NOT NULL,
    result_sha256 CHAR(64) NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_ref VARCHAR(240) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_routine_budget_exception_command
        FOREIGN KEY (command_id, routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routine_advanced_commands
            (command_id, routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_routine_budget_exception_owner
        UNIQUE (command_id, routine_id, tenant_id, user_id),
    CONSTRAINT ck_ai_routine_budget_exception_effect CHECK (
        additional_runs > 0 OR additional_tokens_per_run > 0
            OR additional_minutes_per_run > 0
    ),
    CONSTRAINT ck_ai_routine_budget_exception_expiry CHECK (expires_at > created_at)
);

CREATE INDEX idx_ai_routine_budget_exception_active
    ON ai_personal_routine_budget_exceptions
        (tenant_id, user_id, routine_id, expires_at, command_id);

CREATE TRIGGER trg_ai_routine_budget_exceptions_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_budget_exceptions
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_personal_routine_budget_run_consumptions (
    routine_run_id UUID PRIMARY KEY,
    exception_command_id UUID NOT NULL,
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    budget_month DATE NOT NULL,
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_routine_budget_consumption_run
        FOREIGN KEY (routine_run_id, routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routine_executions
            (routine_run_id, routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_ai_routine_budget_consumption_exception
        FOREIGN KEY (exception_command_id, routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routine_budget_exceptions
            (command_id, routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_routine_budget_consumption_month CHECK (
        budget_month = date_trunc('month', budget_month)::date
    )
);

CREATE INDEX idx_ai_routine_budget_consumption_exception
    ON ai_personal_routine_budget_run_consumptions
        (exception_command_id, budget_month, consumed_at);

CREATE TRIGGER trg_ai_routine_budget_consumptions_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_budget_run_consumptions
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_personal_routine_engine_override_events IS
    'Append-only, versioned engine override state. The latest unexpired APPLY is consumed by the routine worker; ROLLBACK restores the routine default.';
COMMENT ON TABLE ai_personal_routine_budget_exceptions IS
    'Attested, expiring routine budget increases produced by successful advanced commands.';
COMMENT ON TABLE ai_personal_routine_budget_run_consumptions IS
    'Atomic monthly-run reservations consumed from active temporary budget exceptions.';
