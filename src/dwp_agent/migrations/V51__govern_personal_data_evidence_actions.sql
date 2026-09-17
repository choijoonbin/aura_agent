CREATE TABLE ai_personal_data_evidence_commands (
    command_id UUID PRIMARY KEY,
    deletion_job_id UUID NOT NULL
        REFERENCES ai_data_deletion_jobs(deletion_job_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    action VARCHAR(40) NOT NULL,
    expected_revision INTEGER NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    evidence_digest CHAR(64) NOT NULL,
    request_envelope TEXT NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    receipt_id UUID,
    provider_receipt_id VARCHAR(240),
    result_envelope TEXT,
    result_fingerprint CHAR(64),
    safe_error_code VARCHAR(128),
    recovery_hint VARCHAR(1000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_personal_data_evidence_owner_command
        UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_personal_data_evidence_action CHECK (
        action IN ('BACKUP_LEDGER', 'SRE_ESCALATION', 'LEGAL_HOLD_APPEAL',
                   'SIGNED_CERTIFICATE', 'SIEM_SYNC')
    ),
    CONSTRAINT ck_ai_personal_data_evidence_revision CHECK (expected_revision >= 0),
    CONSTRAINT ck_ai_personal_data_evidence_session CHECK (
        session_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_personal_data_evidence_request CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_personal_data_evidence_digest CHECK (
        evidence_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_personal_data_evidence_request_envelope CHECK (
        request_envelope LIKE 'dwp2.%'
    ),
    CONSTRAINT ck_ai_personal_data_evidence_result_envelope CHECK (
        result_envelope IS NULL OR result_envelope LIKE 'dwp2.%'
    ),
    CONSTRAINT ck_ai_personal_data_evidence_state CHECK (
        state IN ('PENDING', 'COMPLETED', 'FAILED')
    ),
    CONSTRAINT ck_ai_personal_data_evidence_terminal CHECK (
        (state = 'PENDING'
            AND receipt_id IS NULL
            AND provider_receipt_id IS NULL
            AND result_envelope IS NULL
            AND result_fingerprint IS NULL
            AND safe_error_code IS NULL
            AND recovery_hint IS NULL
            AND completed_at IS NULL)
        OR
        (state = 'COMPLETED'
            AND receipt_id IS NOT NULL
            AND provider_receipt_id IS NOT NULL
            AND result_envelope IS NOT NULL
            AND result_fingerprint ~ '^[0-9a-f]{64}$'
            AND safe_error_code IS NULL
            AND recovery_hint IS NULL
            AND completed_at IS NOT NULL)
        OR
        (state = 'FAILED'
            AND receipt_id IS NOT NULL
            AND result_envelope IS NOT NULL
            AND result_fingerprint ~ '^[0-9a-f]{64}$'
            AND safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
            AND recovery_hint IS NOT NULL
            AND completed_at IS NOT NULL)
    )
);

CREATE INDEX idx_ai_personal_data_evidence_owner
    ON ai_personal_data_evidence_commands
        (tenant_id, user_id, deletion_job_id, created_at DESC);

CREATE TABLE ai_personal_data_evidence_command_events (
    event_id UUID PRIMARY KEY,
    command_id UUID NOT NULL
        REFERENCES ai_personal_data_evidence_commands(command_id) ON DELETE RESTRICT,
    deletion_job_id UUID NOT NULL
        REFERENCES ai_data_deletion_jobs(deletion_job_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    action VARCHAR(40) NOT NULL,
    event_type VARCHAR(16) NOT NULL,
    safe_error_code VARCHAR(128),
    evidence_fingerprint CHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_personal_data_evidence_event_action CHECK (
        action IN ('BACKUP_LEDGER', 'SRE_ESCALATION', 'LEGAL_HOLD_APPEAL',
                   'SIGNED_CERTIFICATE', 'SIEM_SYNC')
    ),
    CONSTRAINT ck_ai_personal_data_evidence_event_type CHECK (
        event_type IN ('REQUESTED', 'COMPLETED', 'FAILED')
    ),
    CONSTRAINT ck_ai_personal_data_evidence_event_fingerprint CHECK (
        evidence_fingerprint ~ '^[0-9a-f]{64}$'
    )
);

CREATE TRIGGER trg_ai_personal_data_evidence_events_append_only
BEFORE UPDATE OR DELETE ON ai_personal_data_evidence_command_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_personal_data_evidence_download_events (
    receipt_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    deletion_job_id UUID
        REFERENCES ai_data_deletion_jobs(deletion_job_id) ON DELETE RESTRICT,
    command_id UUID
        REFERENCES ai_personal_data_evidence_commands(command_id) ON DELETE RESTRICT,
    download_type VARCHAR(32) NOT NULL,
    filename VARCHAR(240) NOT NULL,
    byte_count BIGINT NOT NULL,
    evidence_fingerprint CHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_personal_data_evidence_download_type CHECK (
        download_type IN ('LEGAL_HOLD_SNAPSHOT', 'RECEIPT_INDEX', 'SIGNED_CERTIFICATE')
    ),
    CONSTRAINT ck_ai_personal_data_evidence_download_count CHECK (byte_count > 0),
    CONSTRAINT ck_ai_personal_data_evidence_download_fingerprint CHECK (
        evidence_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_personal_data_evidence_download_binding CHECK (
        (download_type = 'RECEIPT_INDEX'
            AND deletion_job_id IS NULL AND command_id IS NULL)
        OR
        (download_type = 'LEGAL_HOLD_SNAPSHOT'
            AND deletion_job_id IS NOT NULL AND command_id IS NULL)
        OR
        (download_type = 'SIGNED_CERTIFICATE'
            AND deletion_job_id IS NOT NULL AND command_id IS NOT NULL)
    )
);

CREATE TRIGGER trg_ai_personal_data_evidence_download_events_append_only
BEFORE UPDATE OR DELETE ON ai_personal_data_evidence_download_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();
