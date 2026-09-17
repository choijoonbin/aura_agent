CREATE TABLE ai_personal_routine_advanced_commands (
    command_id UUID PRIMARY KEY,
    routine_id UUID NOT NULL REFERENCES ai_personal_routines(routine_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    kind VARCHAR(48) NOT NULL,
    state VARCHAR(32) NOT NULL,
    expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    maker_user_id VARCHAR(160) NOT NULL,
    checker_user_id VARCHAR(160),
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    payload_envelope TEXT NOT NULL,
    problem_envelope TEXT,
    receipt_envelope TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_routine_advanced_owner UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_routine_advanced_kind CHECK (kind IN (
        'CHANGE_APPROVAL', 'AGENT_ENGINE_SWITCH', 'WORM_EVIDENCE_DELIVERY',
        'OAUTH_REAUTHORIZATION', 'TEMPORARY_BUDGET_INCREASE',
        'OPERATOR_ESCALATION', 'PROVIDER_ROLLBACK'
    )),
    CONSTRAINT ck_ai_routine_advanced_state CHECK (state IN (
        'AWAITING_APPROVAL', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'REJECTED'
    )),
    CONSTRAINT ck_ai_routine_advanced_fingerprints CHECK (
        session_fingerprint ~ '^[0-9a-f]{64}$'
        AND request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_routine_advanced_envelopes CHECK (
        payload_envelope LIKE 'dwp2.%'
        AND (problem_envelope IS NULL OR problem_envelope LIKE 'dwp2.%')
        AND (receipt_envelope IS NULL OR receipt_envelope LIKE 'dwp2.%')
    )
);

CREATE INDEX idx_ai_routine_advanced_routine
    ON ai_personal_routine_advanced_commands
    (tenant_id, user_id, routine_id, created_at DESC);

CREATE TABLE ai_personal_routine_advanced_events (
    event_id UUID PRIMARY KEY,
    command_id UUID NOT NULL
        REFERENCES ai_personal_routine_advanced_commands(command_id) ON DELETE RESTRICT,
    routine_id UUID NOT NULL REFERENCES ai_personal_routines(routine_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    event_type VARCHAR(48) NOT NULL,
    previous_state VARCHAR(32),
    current_state VARCHAR(32) NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ai_routine_advanced_events_command
    ON ai_personal_routine_advanced_events (command_id, occurred_at, event_id);

CREATE TRIGGER trg_ai_personal_routine_advanced_events_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_advanced_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_personal_routine_advanced_commands IS
    'Tenant-bound maker-checker and attested provider commands for advanced routine controls.';
