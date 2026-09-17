CREATE TABLE ai_proposal_handoffs (
    handoff_id UUID PRIMARY KEY,
    proposal_id UUID NOT NULL REFERENCES ai_agent_proposals(proposal_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    idempotency_key UUID NOT NULL,
    action_key VARCHAR(128) NOT NULL,
    target_route VARCHAR(1000) NOT NULL,
    handoff_state VARCHAR(32) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    approval_required BOOLEAN NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    reviewed_inputs_envelope TEXT NOT NULL,
    receipt_id UUID,
    receipt_envelope TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_ai_proposal_handoff_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_proposal_handoff_idempotency UNIQUE (tenant_id, user_id, idempotency_key),
    CONSTRAINT uq_ai_proposal_handoff_proposal UNIQUE (proposal_id, tenant_id, user_id),
    CONSTRAINT ck_ai_proposal_handoff_state CHECK (
        handoff_state IN ('REVIEW_REQUIRED', 'AWAITING_APPROVAL', 'HANDED_OFF',
                          'RUNNING', 'PARTIAL', 'COMPLETED', 'FAILED',
                          'CANCELLED', 'COMPENSATING', 'COMPENSATED')
    ),
    CONSTRAINT ck_ai_proposal_handoff_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_proposal_handoff_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_proposal_handoff_inputs CHECK (reviewed_inputs_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_proposal_handoff_receipt CHECK (
        (receipt_id IS NULL AND receipt_envelope IS NULL AND completed_at IS NULL)
        OR (receipt_id IS NOT NULL AND receipt_envelope LIKE 'dwp2.%')
    )
);

CREATE INDEX idx_ai_proposal_handoffs_owner
    ON ai_proposal_handoffs (tenant_id, user_id, updated_at DESC);

CREATE TABLE ai_proposal_handoff_events (
    event_id UUID PRIMARY KEY,
    handoff_id UUID NOT NULL REFERENCES ai_proposal_handoffs(handoff_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    previous_state VARCHAR(32),
    current_state VARCHAR(32) NOT NULL,
    revision INTEGER NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_proposal_handoff_event_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_proposal_handoff_event_revision CHECK (revision > 0)
);

CREATE TRIGGER trg_ai_proposal_handoff_events_append_only
BEFORE UPDATE OR DELETE ON ai_proposal_handoff_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_secure_attachments (
    attachment_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    conversation_id UUID,
    command_id UUID NOT NULL,
    attachment_state VARCHAR(24) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    size_bytes BIGINT NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    descriptor_envelope TEXT NOT NULL,
    upload_reference_envelope TEXT,
    stage_results_envelope TEXT NOT NULL,
    citation_manifest_envelope TEXT,
    retention_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMPTZ,
    CONSTRAINT uq_ai_secure_attachment_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_secure_attachment_state CHECK (
        attachment_state IN ('UPLOADING', 'SCANNING', 'READY', 'PARTIAL',
                             'BLOCKED', 'FAILED', 'CANCELLED',
                             'DELETION_PENDING', 'DELETED')
    ),
    CONSTRAINT ck_ai_secure_attachment_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_secure_attachment_size CHECK (size_bytes BETWEEN 1 AND 104857600),
    CONSTRAINT ck_ai_secure_attachment_sha CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_secure_attachment_descriptor CHECK (descriptor_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_secure_attachment_upload CHECK (
        upload_reference_envelope IS NULL OR upload_reference_envelope LIKE 'dwp2.%'
    ),
    CONSTRAINT ck_ai_secure_attachment_stages CHECK (stage_results_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_secure_attachment_citations CHECK (
        citation_manifest_envelope IS NULL OR citation_manifest_envelope LIKE 'dwp2.%'
    ),
    CONSTRAINT ck_ai_secure_attachment_retention CHECK (retention_expires_at > created_at),
    CONSTRAINT ck_ai_secure_attachment_deleted CHECK (
        (attachment_state = 'DELETED' AND deleted_at IS NOT NULL)
        OR (attachment_state <> 'DELETED')
    )
);

CREATE INDEX idx_ai_secure_attachments_owner
    ON ai_secure_attachments (tenant_id, user_id, updated_at DESC)
    WHERE attachment_state <> 'DELETED';

CREATE TABLE ai_secure_attachment_events (
    event_id UUID PRIMARY KEY,
    attachment_id UUID NOT NULL REFERENCES ai_secure_attachments(attachment_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    previous_state VARCHAR(24),
    current_state VARCHAR(24) NOT NULL,
    revision INTEGER NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    safe_error_code VARCHAR(128),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_secure_attachment_event_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_secure_attachment_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_secure_attachment_event_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_secure_attachment_event_error CHECK (
        safe_error_code IS NULL OR safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
    )
);

CREATE TRIGGER trg_ai_secure_attachment_events_append_only
BEFORE UPDATE OR DELETE ON ai_secure_attachment_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_research_plans (
    plan_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    plan_state VARCHAR(24) NOT NULL DEFAULT 'DRAFT',
    revision INTEGER NOT NULL DEFAULT 1,
    definition_envelope TEXT NOT NULL,
    definition_fingerprint CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_research_plan_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_research_plan_owner UNIQUE (plan_id, tenant_id, user_id),
    CONSTRAINT ck_ai_research_plan_state CHECK (plan_state IN ('DRAFT', 'READY', 'ARCHIVED')),
    CONSTRAINT ck_ai_research_plan_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_research_plan_definition CHECK (definition_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_research_plan_fingerprint CHECK (definition_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX idx_ai_research_plans_owner
    ON ai_research_plans (tenant_id, user_id, updated_at DESC);

CREATE TABLE ai_research_plan_commands (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    plan_id UUID NOT NULL,
    command_type VARCHAR(16) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id, command_id),
    CONSTRAINT fk_ai_research_plan_command
        FOREIGN KEY (plan_id, tenant_id, user_id)
        REFERENCES ai_research_plans(plan_id, tenant_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT ck_ai_research_plan_command_type CHECK (command_type IN ('CREATE', 'UPDATE')),
    CONSTRAINT ck_ai_research_plan_command_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TRIGGER trg_ai_research_plan_commands_append_only
BEFORE UPDATE OR DELETE ON ai_research_plan_commands
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_research_runs (
    run_id UUID PRIMARY KEY,
    plan_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    idempotency_key UUID NOT NULL,
    run_state VARCHAR(32) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    plan_revision INTEGER NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    progress_envelope TEXT NOT NULL,
    result_envelope TEXT,
    receipt_id UUID,
    safe_error_code VARCHAR(128),
    started_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT fk_ai_research_run_plan
        FOREIGN KEY (plan_id, tenant_id, user_id)
        REFERENCES ai_research_plans(plan_id, tenant_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_ai_research_run_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_research_run_idempotency UNIQUE (tenant_id, user_id, idempotency_key),
    CONSTRAINT ck_ai_research_run_state CHECK (
        run_state IN ('QUEUED', 'RUNNING', 'PAUSED', 'PARTIAL', 'CONFLICT',
                      'CANCELLING', 'CANCELLED', 'FAILED', 'COMPLETED')
    ),
    CONSTRAINT ck_ai_research_run_revision CHECK (revision > 0 AND plan_revision > 0),
    CONSTRAINT ck_ai_research_run_generation CHECK (generation >= 0),
    CONSTRAINT ck_ai_research_run_progress CHECK (progress_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_research_run_result CHECK (
        result_envelope IS NULL OR result_envelope LIKE 'dwp2.%'
    ),
    CONSTRAINT ck_ai_research_run_error CHECK (
        safe_error_code IS NULL OR safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
    ),
    CONSTRAINT ck_ai_research_run_receipt CHECK (
        (run_state = 'COMPLETED' AND receipt_id IS NOT NULL AND result_envelope IS NOT NULL
            AND completed_at IS NOT NULL)
        OR (run_state <> 'COMPLETED')
    )
);

CREATE INDEX idx_ai_research_runs_owner
    ON ai_research_runs (tenant_id, user_id, updated_at DESC);
CREATE INDEX idx_ai_research_runs_worker
    ON ai_research_runs (run_state, created_at)
    WHERE run_state IN ('QUEUED', 'RUNNING', 'CANCELLING');

CREATE TABLE ai_research_run_events (
    event_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ai_research_runs(run_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(48) NOT NULL,
    previous_state VARCHAR(32),
    current_state VARCHAR(32) NOT NULL,
    revision INTEGER NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    detail_envelope TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_research_run_event_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_research_run_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_research_run_event_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_research_run_event_detail CHECK (
        detail_envelope IS NULL OR detail_envelope LIKE 'dwp2.%'
    )
);

CREATE TRIGGER trg_ai_research_run_events_append_only
BEFORE UPDATE OR DELETE ON ai_research_run_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_research_deliveries (
    delivery_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ai_research_runs(run_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    idempotency_key UUID NOT NULL,
    delivery_type VARCHAR(24) NOT NULL,
    delivery_state VARCHAR(32) NOT NULL,
    request_envelope TEXT NOT NULL,
    receipt_envelope TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_ai_research_delivery_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_research_delivery_idempotency UNIQUE (tenant_id, user_id, idempotency_key),
    CONSTRAINT ck_ai_research_delivery_type CHECK (
        delivery_type IN ('ARTIFACT', 'PROPOSAL', 'EXPORT', 'HANDOFF', 'SHARE', 'ROUTINE')
    ),
    CONSTRAINT ck_ai_research_delivery_state CHECK (
        delivery_state IN ('QUEUED', 'AWAITING_APPROVAL', 'RUNNING', 'PARTIAL',
                           'COMPLETED', 'FAILED', 'CANCELLED')
    ),
    CONSTRAINT ck_ai_research_delivery_request CHECK (request_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_research_delivery_receipt CHECK (
        receipt_envelope IS NULL OR receipt_envelope LIKE 'dwp2.%'
    )
);

COMMENT ON TABLE ai_secure_attachments IS
    'User-owned attachment metadata and governed scan/parser state. File bytes remain in the approved storage provider.';
COMMENT ON TABLE ai_research_runs IS
    'Deep Research state ledger. COMPLETED is allowed only with an encrypted result and receipt ID.';
COMMENT ON TABLE ai_research_deliveries IS
    'Requested downstream work. A queued row is never proof that the target system completed the action.';
