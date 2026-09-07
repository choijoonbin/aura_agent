CREATE TABLE ai_personal_routines (
    routine_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    lifecycle_state VARCHAR(24) NOT NULL DEFAULT 'DRAFT',
    consent_state VARCHAR(32) NOT NULL DEFAULT 'UNSET',
    execution_mode VARCHAR(24) NOT NULL DEFAULT 'DRY_RUN_ONLY',
    revision INTEGER NOT NULL DEFAULT 1,
    definition_envelope TEXT NOT NULL,
    definition_fingerprint CHAR(64) NOT NULL,
    next_run_at TIMESTAMPTZ,
    retention_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TIMESTAMPTZ,
    CONSTRAINT uq_ai_personal_routine_owner UNIQUE (routine_id, tenant_id, user_id),
    CONSTRAINT ck_ai_personal_routine_lifecycle CHECK (
        lifecycle_state IN ('DRAFT', 'PAUSED', 'ARCHIVED')
    ),
    CONSTRAINT ck_ai_personal_routine_consent CHECK (
        consent_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
    ),
    CONSTRAINT ck_ai_personal_routine_mode CHECK (execution_mode = 'DRY_RUN_ONLY'),
    CONSTRAINT ck_ai_personal_routine_not_scheduled CHECK (next_run_at IS NULL),
    CONSTRAINT ck_ai_personal_routine_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_personal_routine_definition CHECK (definition_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_personal_routine_fingerprint CHECK (
        definition_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_personal_routine_archive CHECK (
        (lifecycle_state = 'ARCHIVED' AND archived_at IS NOT NULL)
        OR (lifecycle_state <> 'ARCHIVED' AND archived_at IS NULL)
    )
);

CREATE INDEX idx_ai_personal_routines_owner
    ON ai_personal_routines (tenant_id, user_id, updated_at DESC)
    WHERE lifecycle_state <> 'ARCHIVED';

CREATE TABLE ai_personal_routine_sources (
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    source_key VARCHAR(32) NOT NULL,
    required_permission VARCHAR(128) NOT NULL,
    PRIMARY KEY (routine_id, source_key),
    CONSTRAINT fk_ai_personal_routine_source_owner
        FOREIGN KEY (routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routines (routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_personal_routine_source CHECK (
        source_key IN ('WORK_ITEM', 'MAIL', 'CALENDAR')
    )
);

CREATE TABLE ai_personal_routine_commands (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    routine_id UUID NOT NULL,
    command_type VARCHAR(24) NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    result_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id, command_id),
    CONSTRAINT fk_ai_personal_routine_command_owner
        FOREIGN KEY (routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routines (routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_personal_routine_command_type CHECK (
        command_type IN ('CREATE', 'UPDATE', 'CONSENT', 'DRY_RUN', 'ARCHIVE')
    ),
    CONSTRAINT ck_ai_personal_routine_command_session CHECK (
        session_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_personal_routine_command_request CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_personal_routine_command_result CHECK (result_envelope LIKE 'dwp2.%')
);

CREATE TRIGGER trg_ai_personal_routine_commands_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_commands
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_personal_routine_consents (
    consent_event_id UUID PRIMARY KEY,
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    previous_state VARCHAR(32) NOT NULL,
    current_state VARCHAR(32) NOT NULL,
    routine_revision INTEGER NOT NULL,
    scope_fingerprint CHAR(64) NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    change_reason_envelope TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_personal_routine_consent_owner
        FOREIGN KEY (routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routines (routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_personal_routine_consent_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_personal_routine_consent_states CHECK (
        previous_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
        AND current_state IN ('DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
    ),
    CONSTRAINT ck_ai_personal_routine_consent_revision CHECK (routine_revision > 0),
    CONSTRAINT ck_ai_personal_routine_consent_scope CHECK (scope_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_personal_routine_consent_session CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_personal_routine_consent_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_personal_routine_consent_reason CHECK (reason_code ~ '^[A-Z][A-Z0-9_.-]{1,63}$'),
    CONSTRAINT ck_ai_personal_routine_consent_reason_envelope CHECK (
        change_reason_envelope LIKE 'dwp2.%'
    )
);

CREATE TRIGGER trg_ai_personal_routine_consents_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_consents
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_personal_routine_runs (
    routine_run_id UUID PRIMARY KEY,
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    trigger_type VARCHAR(16) NOT NULL,
    run_state VARCHAR(24) NOT NULL,
    routine_revision INTEGER NOT NULL,
    source_count INTEGER NOT NULL,
    proposal_only BOOLEAN NOT NULL DEFAULT TRUE,
    preview_next_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_personal_routine_run_owner
        FOREIGN KEY (routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routines (routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_personal_routine_run_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_personal_routine_run_trigger CHECK (trigger_type = 'DRY_RUN'),
    CONSTRAINT ck_ai_personal_routine_run_state CHECK (run_state = 'VALIDATED'),
    CONSTRAINT ck_ai_personal_routine_run_revision CHECK (routine_revision > 0),
    CONSTRAINT ck_ai_personal_routine_run_sources CHECK (source_count BETWEEN 1 AND 3),
    CONSTRAINT ck_ai_personal_routine_run_proposal_only CHECK (proposal_only = TRUE),
    CONSTRAINT ck_ai_personal_routine_run_completed CHECK (completed_at >= created_at)
);

CREATE TRIGGER trg_ai_personal_routine_runs_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_runs
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_personal_routine_events (
    event_id UUID PRIMARY KEY,
    routine_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(24) NOT NULL,
    previous_state VARCHAR(32),
    current_state VARCHAR(32) NOT NULL,
    revision INTEGER NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    change_reason_envelope TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_personal_routine_event_owner
        FOREIGN KEY (routine_id, tenant_id, user_id)
        REFERENCES ai_personal_routines (routine_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_personal_routine_event_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_personal_routine_event_type CHECK (
        event_type IN ('CREATED', 'UPDATED', 'CONSENT_CHANGED', 'DRY_RUN_VALIDATED', 'ARCHIVED')
    ),
    CONSTRAINT ck_ai_personal_routine_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_personal_routine_event_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_personal_routine_event_reason CHECK (reason_code ~ '^[A-Z][A-Z0-9_.-]{1,63}$'),
    CONSTRAINT ck_ai_personal_routine_event_reason_envelope CHECK (
        change_reason_envelope IS NULL OR change_reason_envelope LIKE 'dwp2.%'
    )
);

CREATE TRIGGER trg_ai_personal_routine_events_append_only
BEFORE UPDATE OR DELETE ON ai_personal_routine_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_personal_routines IS
    'User-owned routine definitions. V24 is intentionally DRY_RUN_ONLY and never schedules work.';
COMMENT ON TABLE ai_personal_routine_consents IS
    'Explicit consent evidence. Proposal-analysis opt-out is not background routine consent.';
