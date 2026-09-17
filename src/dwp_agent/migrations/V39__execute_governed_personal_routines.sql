ALTER TABLE ai_personal_routines
    DROP CONSTRAINT ck_ai_personal_routine_lifecycle,
    DROP CONSTRAINT ck_ai_personal_routine_mode,
    DROP CONSTRAINT ck_ai_personal_routine_not_scheduled;

ALTER TABLE ai_personal_routines
    ADD CONSTRAINT ck_ai_personal_routine_lifecycle CHECK (
        lifecycle_state IN ('DRAFT', 'ACTIVE', 'PAUSED', 'ARCHIVED')
    ),
    ADD CONSTRAINT ck_ai_personal_routine_mode CHECK (
        execution_mode IN ('DRY_RUN_ONLY', 'SCHEDULED')
    ),
    ADD CONSTRAINT ck_ai_personal_routine_schedule_coherence CHECK (
        (lifecycle_state = 'ACTIVE'
            AND execution_mode = 'SCHEDULED'
            AND next_run_at IS NOT NULL)
        OR (lifecycle_state <> 'ACTIVE' AND next_run_at IS NULL)
    );

ALTER TABLE ai_personal_routine_commands
    DROP CONSTRAINT ck_ai_personal_routine_command_type;

ALTER TABLE ai_personal_routine_commands
    ADD CONSTRAINT ck_ai_personal_routine_command_type CHECK (
        command_type IN (
            'CREATE', 'UPDATE', 'CONSENT', 'DRY_RUN', 'LIFECYCLE', 'ARCHIVE',
            'ACTIVATION', 'TRIGGER_RUN', 'RUN_COMMAND'
        )
    );

ALTER TABLE ai_personal_routine_events
    ALTER COLUMN event_type TYPE VARCHAR(40),
    DROP CONSTRAINT ck_ai_personal_routine_event_type;

ALTER TABLE ai_personal_routine_events
    ADD CONSTRAINT ck_ai_personal_routine_event_type CHECK (
        event_type IN (
            'CREATED', 'UPDATED', 'CONSENT_CHANGED', 'DRY_RUN_VALIDATED',
            'LIFECYCLE_CHANGED', 'ARCHIVED', 'ACTIVATED', 'DEACTIVATED',
            'RUN_TRIGGERED', 'RUN_RETRY_REQUESTED', 'RUN_CANCELLED',
            'RUN_COMPENSATION_REQUESTED'
        )
    );

CREATE TABLE ai_personal_routine_executions (
    routine_run_id UUID PRIMARY KEY,
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    routine_revision INTEGER NOT NULL,
    trigger_type VARCHAR(16) NOT NULL,
    run_state VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
    version INTEGER NOT NULL DEFAULT 1,
    scheduled_for TIMESTAMPTZ NOT NULL,
    next_attempt_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    maximum_attempts INTEGER NOT NULL,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    correlation_id VARCHAR(160) NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    proposals_created INTEGER NOT NULL DEFAULT 0,
    approval_gated_actions_created INTEGER NOT NULL DEFAULT 0,
    external_writes_performed INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    notification_state VARCHAR(32) NOT NULL DEFAULT 'NOT_REQUIRED',
    provider_receipt_envelope TEXT,
    result_sha256 CHAR(64),
    receipt_id UUID,
    receipt_fingerprint CHAR(64),
    safe_error_code VARCHAR(128),
    recovery_hint VARCHAR(1000),
    compensation_required BOOLEAN NOT NULL DEFAULT FALSE,
    compensation_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT fk_ai_personal_routine_execution_owner
        FOREIGN KEY (routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routines (routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_personal_routine_execution_slot
        UNIQUE (routine_id, routine_revision, scheduled_for, trigger_type),
    CONSTRAINT ck_ai_personal_routine_execution_trigger CHECK (
        trigger_type IN ('SCHEDULED', 'MANUAL')
    ),
    CONSTRAINT ck_ai_personal_routine_execution_state CHECK (
        run_state IN (
            'QUEUED', 'CLAIMED', 'RUNNING', 'RETRY_SCHEDULED', 'COMPENSATING',
            'PARTIAL', 'COMPLETED', 'FAILED', 'CANCELLED', 'COMPENSATED'
        )
    ),
    CONSTRAINT ck_ai_personal_routine_execution_revision CHECK (
        routine_revision > 0 AND version > 0
    ),
    CONSTRAINT ck_ai_personal_routine_execution_attempt CHECK (
        attempt_count BETWEEN 0 AND maximum_attempts
        AND maximum_attempts BETWEEN 1 AND 10
    ),
    CONSTRAINT ck_ai_personal_routine_execution_lease CHECK (
        (run_state IN ('CLAIMED', 'RUNNING', 'COMPENSATING')
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (run_state NOT IN ('CLAIMED', 'RUNNING', 'COMPENSATING')
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_ai_personal_routine_execution_counts CHECK (
        evidence_count >= 0 AND proposals_created >= 0
        AND approval_gated_actions_created >= 0
        AND external_writes_performed = 0
        AND tokens_used >= 0 AND elapsed_ms >= 0
    ),
    CONSTRAINT ck_ai_personal_routine_execution_notification CHECK (
        notification_state IN ('NOT_REQUIRED', 'DELIVERED', 'NOT_CONFIGURED', 'FAILED')
    ),
    CONSTRAINT ck_ai_personal_routine_execution_provider_receipt CHECK (
        provider_receipt_envelope IS NULL OR provider_receipt_envelope LIKE 'dwp2.%'
    ),
    CONSTRAINT ck_ai_personal_routine_execution_result CHECK (
        result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_personal_routine_execution_receipt CHECK (
        (run_state IN ('COMPLETED', 'COMPENSATED')
            AND completed_at IS NOT NULL
            AND receipt_id IS NOT NULL
            AND receipt_fingerprint ~ '^[0-9a-f]{64}$'
            AND provider_receipt_envelope IS NOT NULL
            AND result_sha256 IS NOT NULL)
        OR (run_state IN ('PARTIAL', 'FAILED', 'CANCELLED')
            AND completed_at IS NOT NULL
            AND receipt_id IS NULL
            AND receipt_fingerprint IS NULL)
        OR (run_state NOT IN ('PARTIAL', 'COMPLETED', 'FAILED', 'CANCELLED', 'COMPENSATED')
            AND completed_at IS NULL
            AND receipt_id IS NULL
            AND receipt_fingerprint IS NULL)
    ),
    CONSTRAINT ck_ai_personal_routine_execution_error CHECK (
        safe_error_code IS NULL
        OR safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
    )
);

CREATE INDEX idx_ai_personal_routine_executions_owner
    ON ai_personal_routine_executions
        (tenant_id, user_id, routine_id, created_at DESC);

CREATE INDEX idx_ai_personal_routine_executions_claim
    ON ai_personal_routine_executions
        (COALESCE(next_attempt_at, scheduled_for), created_at)
    WHERE run_state IN ('QUEUED', 'RETRY_SCHEDULED', 'COMPENSATING');

CREATE INDEX idx_ai_personal_routines_due
    ON ai_personal_routines (next_run_at, routine_id)
    WHERE lifecycle_state = 'ACTIVE' AND execution_mode = 'SCHEDULED';

CREATE TABLE ai_personal_routine_execution_events (
    event_id UUID PRIMARY KEY,
    routine_run_id UUID NOT NULL,
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    previous_state VARCHAR(32),
    current_state VARCHAR(32) NOT NULL,
    version INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    lease_generation INTEGER NOT NULL,
    safe_error_code VARCHAR(128),
    receipt_fingerprint CHAR(64),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_personal_routine_execution_event
        FOREIGN KEY (routine_run_id)
        REFERENCES ai_personal_routine_executions (routine_run_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_personal_routine_execution_event_state CHECK (
        current_state IN (
            'QUEUED', 'CLAIMED', 'RUNNING', 'RETRY_SCHEDULED', 'COMPENSATING',
            'PARTIAL', 'COMPLETED', 'FAILED', 'CANCELLED', 'COMPENSATED'
        )
    ),
    CONSTRAINT ck_ai_personal_routine_execution_event_version CHECK (
        version > 0 AND attempt_count >= 0 AND lease_generation >= 0
    ),
    CONSTRAINT ck_ai_personal_routine_execution_event_error CHECK (
        safe_error_code IS NULL
        OR safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
    ),
    CONSTRAINT ck_ai_personal_routine_execution_event_receipt CHECK (
        receipt_fingerprint IS NULL OR receipt_fingerprint ~ '^[0-9a-f]{64}$'
    )
);

CREATE TRIGGER trg_ai_personal_routine_execution_events_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_execution_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_personal_routine_executions IS
    'Governed routine scheduler and execution truth. External writes are never performed directly; actions are approval gated.';
COMMENT ON COLUMN ai_personal_routine_executions.provider_receipt_envelope IS
    'Encrypted provider receipt identifier. A success receipt is sealed only for COMPLETED or COMPENSATED.';
