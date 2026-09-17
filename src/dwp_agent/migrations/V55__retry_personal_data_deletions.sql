CREATE TABLE ai_data_deletion_retry_commands (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    deletion_job_id UUID NOT NULL
        REFERENCES ai_data_deletion_jobs(deletion_job_id) ON DELETE RESTRICT,
    expected_attempt_count INTEGER NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    change_reason_envelope TEXT NOT NULL,
    resulting_state VARCHAR(32) NOT NULL,
    resulting_generation BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_data_deletion_retry_attempt CHECK (expected_attempt_count >= 0),
    CONSTRAINT ck_ai_data_deletion_retry_generation CHECK (resulting_generation >= 0),
    CONSTRAINT ck_ai_data_deletion_retry_fingerprints CHECK (
        session_fingerprint ~ '^[0-9a-f]{64}$'
        AND request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_data_deletion_retry_reason CHECK (
        reason_code ~ '^[A-Z][A-Z0-9_.-]{1,63}$'),
    CONSTRAINT ck_ai_data_deletion_retry_envelope CHECK (
        change_reason_envelope LIKE 'dwp2.%')
);

CREATE TRIGGER trg_ai_data_deletion_retry_commands_append_only
BEFORE UPDATE OR DELETE ON ai_data_deletion_retry_commands
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_data_deletion_retry_commands IS
    'Immutable, session-bound retry commands for failed or partially completed personal-data deletion targets.';
