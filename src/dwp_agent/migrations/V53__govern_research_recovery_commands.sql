CREATE TABLE ai_research_recovery_commands (
    receipt_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ai_research_runs(run_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    idempotency_key UUID NOT NULL,
    recovery_action VARCHAR(48) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    request_envelope TEXT NOT NULL,
    receipt_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_research_recovery_command
        UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_research_recovery_idempotency
        UNIQUE (tenant_id, user_id, idempotency_key),
    CONSTRAINT ck_ai_research_recovery_action CHECK (
        recovery_action IN ('SAVE_AS_FORK', 'PULL_AND_MERGE', 'KEEP_LOCAL',
                            'RECALCULATE_SENSITIVITY', 'USE_CACHE_FALLBACK')
    ),
    CONSTRAINT ck_ai_research_recovery_fingerprint
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_research_recovery_envelopes
        CHECK (request_envelope LIKE 'dwp2.%' AND receipt_envelope LIKE 'dwp2.%')
);

CREATE INDEX idx_ai_research_recovery_owner
    ON ai_research_recovery_commands (tenant_id, user_id, run_id, created_at DESC);

CREATE TRIGGER trg_ai_research_recovery_commands_append_only
BEFORE UPDATE OR DELETE ON ai_research_recovery_commands
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_research_recovery_events (
    event_id UUID PRIMARY KEY,
    receipt_id UUID NOT NULL
        REFERENCES ai_research_recovery_commands(receipt_id) ON DELETE RESTRICT,
    run_id UUID NOT NULL REFERENCES ai_research_runs(run_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    recovery_action VARCHAR(48) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_research_recovery_event_command
        UNIQUE (tenant_id, user_id, command_id)
);

CREATE TRIGGER trg_ai_research_recovery_events_append_only
BEFORE UPDATE OR DELETE ON ai_research_recovery_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_research_recovery_commands IS
    'Tenant-bound idempotent research conflict, sensitivity, and cache recovery receipts.';
