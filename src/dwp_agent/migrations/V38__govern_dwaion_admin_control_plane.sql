CREATE TABLE ai_admin_control_commands (
    command_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    maker_user_id VARCHAR(160) NOT NULL,
    checker_user_id VARCHAR(160),
    correlation_id VARCHAR(160) NOT NULL,
    auth_session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    command_kind VARCHAR(64) NOT NULL,
    target_type VARCHAR(80) NOT NULL,
    target_id VARCHAR(160) NOT NULL,
    expected_target_version INTEGER NOT NULL,
    target_snapshot_hash CHAR(64) NOT NULL,
    command_state VARCHAR(32) NOT NULL,
    approval_required BOOLEAN NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    review_envelope TEXT NOT NULL,
    payload_envelope TEXT NOT NULL,
    decision_envelope TEXT,
    receipt_envelope TEXT,
    problem_envelope TEXT,
    progress_percent NUMERIC(5,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_ai_admin_control_command_tenant UNIQUE (tenant_id, command_id),
    CONSTRAINT ck_ai_admin_control_command_version CHECK (
        expected_target_version >= 0 AND revision >= 1),
    CONSTRAINT ck_ai_admin_control_command_hashes CHECK (
        auth_session_fingerprint ~ '^[0-9a-f]{64}$'
        AND request_fingerprint ~ '^[0-9a-f]{64}$'
        AND target_snapshot_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_admin_control_command_state CHECK (
        command_state IN (
            'AWAITING_APPROVAL', 'QUEUED', 'RUNNING', 'PARTIAL',
            'SUCCEEDED', 'FAILED', 'REJECTED', 'CANCELLED', 'ROLLED_BACK')),
    CONSTRAINT ck_ai_admin_control_command_envelopes CHECK (
        review_envelope LIKE 'dwp2.%' AND payload_envelope LIKE 'dwp2.%'
        AND (decision_envelope IS NULL OR decision_envelope LIKE 'dwp2.%')
        AND (receipt_envelope IS NULL OR receipt_envelope LIKE 'dwp2.%')
        AND (problem_envelope IS NULL OR problem_envelope LIKE 'dwp2.%')),
    CONSTRAINT ck_ai_admin_control_command_checker CHECK (
        checker_user_id IS NULL OR checker_user_id <> maker_user_id),
    CONSTRAINT ck_ai_admin_control_command_progress CHECK (
        progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100),
    CONSTRAINT ck_ai_admin_control_command_completion CHECK (
        (command_state IN ('SUCCEEDED', 'ROLLED_BACK')
            AND completed_at IS NOT NULL AND receipt_envelope IS NOT NULL)
        OR (command_state NOT IN ('SUCCEEDED', 'ROLLED_BACK')
            AND completed_at IS NULL))
);

CREATE INDEX idx_ai_admin_control_commands_queue
    ON ai_admin_control_commands (tenant_id, command_state, created_at DESC);

CREATE INDEX idx_ai_admin_control_commands_target
    ON ai_admin_control_commands (tenant_id, target_type, target_id, created_at DESC);

CREATE TABLE ai_admin_control_command_events (
    event_id UUID PRIMARY KEY,
    command_id UUID NOT NULL REFERENCES ai_admin_control_commands(command_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    transition_command_id UUID NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    previous_state VARCHAR(32),
    current_state VARCHAR(32) NOT NULL,
    revision INTEGER NOT NULL,
    evidence_envelope TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_admin_control_transition UNIQUE (tenant_id, transition_command_id),
    CONSTRAINT ck_ai_admin_control_event_revision CHECK (revision >= 1),
    CONSTRAINT ck_ai_admin_control_event_evidence CHECK (
        evidence_envelope IS NULL OR evidence_envelope LIKE 'dwp2.%')
);

CREATE TRIGGER trg_ai_admin_control_command_events_append_only
BEFORE UPDATE OR DELETE ON ai_admin_control_command_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_admin_control_resources (
    tenant_id BIGINT NOT NULL,
    resource_type VARCHAR(80) NOT NULL,
    resource_id VARCHAR(160) NOT NULL,
    resource_version INTEGER NOT NULL,
    snapshot_hash CHAR(64) NOT NULL,
    snapshot_envelope TEXT NOT NULL,
    updated_by_command_id UUID NOT NULL
        REFERENCES ai_admin_control_commands(command_id) ON DELETE RESTRICT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, resource_type, resource_id),
    CONSTRAINT ck_ai_admin_control_resource_version CHECK (resource_version >= 1),
    CONSTRAINT ck_ai_admin_control_resource_hash CHECK (
        snapshot_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_admin_control_resource_envelope CHECK (
        snapshot_envelope LIKE 'dwp2.%')
);

COMMENT ON TABLE ai_admin_control_commands IS
    'Encrypted, tenant-scoped maker-checker ledger for DWAI-ON administrative control-plane commands. A queued row is intent, not evidence of an external side effect.';
COMMENT ON TABLE ai_admin_control_command_events IS
    'Immutable command transition and decision evidence with per-attempt idempotency.';
COMMENT ON TABLE ai_admin_control_resources IS
    'Authoritative versions and encrypted snapshots reported only by trusted domain command workers.';
