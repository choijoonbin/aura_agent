CREATE TABLE ai_personal_routine_advanced_decisions (
    decision_id UUID PRIMARY KEY,
    command_id UUID NOT NULL UNIQUE
        REFERENCES ai_personal_routine_advanced_commands(command_id) ON DELETE RESTRICT,
    routine_id UUID NOT NULL REFERENCES ai_personal_routines(routine_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    maker_user_id VARCHAR(160) NOT NULL,
    checker_user_id VARCHAR(160) NOT NULL,
    decision VARCHAR(16) NOT NULL CHECK (decision IN ('APPROVE', 'REJECT')),
    expected_command_version INTEGER NOT NULL CHECK (expected_command_version >= 1),
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    decision_envelope TEXT NOT NULL CHECK (decision_envelope LIKE 'dwp2.%'),
    decided_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_routine_checker_separation CHECK (maker_user_id <> checker_user_id),
    CONSTRAINT ck_ai_routine_decision_fingerprints CHECK (
        session_fingerprint ~ '^[0-9a-f]{64}$'
        AND request_fingerprint ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX idx_ai_routine_advanced_decisions_tenant
    ON ai_personal_routine_advanced_decisions (tenant_id, decided_at DESC, command_id);

CREATE TRIGGER trg_ai_personal_routine_advanced_decisions_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_advanced_decisions
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_personal_routine_advanced_decisions IS
    'Encrypted, append-only checker reasons and evidence bound to a routine change decision.';
