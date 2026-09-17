CREATE TABLE ai_attachment_detach_commands (
    receipt_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    conversation_id UUID NOT NULL,
    command_id UUID NOT NULL,
    idempotency_key UUID NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    receipt_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_attachment_detach_command
        UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_attachment_detach_idempotency
        UNIQUE (tenant_id, user_id, idempotency_key),
    CONSTRAINT ck_ai_attachment_detach_fingerprint
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_attachment_detach_receipt
        CHECK (receipt_envelope LIKE 'dwp2.%')
);

CREATE TRIGGER trg_ai_attachment_detach_commands_append_only
BEFORE UPDATE OR DELETE ON ai_attachment_detach_commands
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_attachment_audit_reports (
    report_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    conversation_id UUID NOT NULL,
    command_id UUID NOT NULL,
    idempotency_key UUID NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    report_envelope TEXT NOT NULL,
    receipt_envelope TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    signature VARCHAR(512) NOT NULL,
    signing_key_fingerprint CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_attachment_audit_report_command
        UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_attachment_audit_report_idempotency
        UNIQUE (tenant_id, user_id, idempotency_key),
    CONSTRAINT ck_ai_attachment_audit_report_request
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_attachment_audit_report_content
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_attachment_audit_report_key
        CHECK (signing_key_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_attachment_audit_report_envelopes
        CHECK (report_envelope LIKE 'dwp2.%' AND receipt_envelope LIKE 'dwp2.%')
);

CREATE INDEX idx_ai_attachment_audit_reports_owner
    ON ai_attachment_audit_reports (tenant_id, user_id, created_at DESC);

CREATE TRIGGER trg_ai_attachment_audit_reports_append_only
BEFORE UPDATE OR DELETE ON ai_attachment_audit_reports
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_attachment_audit_report_events (
    event_id UUID PRIMARY KEY,
    report_id UUID NOT NULL REFERENCES ai_attachment_audit_reports(report_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(48) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_attachment_audit_report_event_command
        UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_attachment_audit_report_event_content
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TRIGGER trg_ai_attachment_audit_report_events_append_only
BEFORE UPDATE OR DELETE ON ai_attachment_audit_report_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_attachment_detach_commands IS
    'Tenant- and conversation-bound idempotent receipts for detaching all selected secure attachments.';
COMMENT ON TABLE ai_attachment_audit_reports IS
    'Encrypted signed security audit reports bound to secure attachment evidence snapshots.';
