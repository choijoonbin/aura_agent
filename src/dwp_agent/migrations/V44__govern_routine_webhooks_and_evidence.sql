ALTER TABLE ai_personal_routines
    DROP CONSTRAINT ck_ai_personal_routine_mode,
    DROP CONSTRAINT ck_ai_personal_routine_schedule_coherence;

ALTER TABLE ai_personal_routines
    ADD CONSTRAINT ck_ai_personal_routine_mode CHECK (
        execution_mode IN ('DRY_RUN_ONLY', 'SCHEDULED', 'WEBHOOK')
    ),
    ADD CONSTRAINT ck_ai_personal_routine_schedule_coherence CHECK (
        (lifecycle_state = 'ACTIVE'
            AND execution_mode = 'SCHEDULED'
            AND next_run_at IS NOT NULL)
        OR (lifecycle_state = 'ACTIVE'
            AND execution_mode = 'WEBHOOK'
            AND next_run_at IS NULL)
        OR (lifecycle_state <> 'ACTIVE'
            AND execution_mode = 'DRY_RUN_ONLY'
            AND next_run_at IS NULL)
    );

ALTER TABLE ai_personal_routine_executions
    DROP CONSTRAINT ck_ai_personal_routine_execution_trigger,
    ADD COLUMN webhook_event_id UUID,
    ADD COLUMN webhook_event_type VARCHAR(64),
    ADD COLUMN webhook_occurred_at TIMESTAMPTZ,
    ADD COLUMN webhook_payload_fingerprint CHAR(64);

ALTER TABLE ai_personal_routine_executions
    ADD CONSTRAINT ck_ai_personal_routine_execution_trigger CHECK (
        trigger_type IN ('SCHEDULED', 'MANUAL', 'WEBHOOK')
    ),
    ADD CONSTRAINT ck_ai_personal_routine_execution_webhook CHECK (
        (trigger_type = 'WEBHOOK'
            AND webhook_event_id IS NOT NULL
            AND webhook_event_type ~ '^[A-Z][A-Z0-9_.-]{1,63}$'
            AND webhook_occurred_at IS NOT NULL
            AND webhook_payload_fingerprint ~ '^[0-9a-f]{64}$')
        OR (trigger_type <> 'WEBHOOK'
            AND webhook_event_id IS NULL
            AND webhook_event_type IS NULL
            AND webhook_occurred_at IS NULL
            AND webhook_payload_fingerprint IS NULL)
    );

CREATE UNIQUE INDEX uq_ai_personal_routine_webhook_event
    ON ai_personal_routine_executions
        (tenant_id, user_id, routine_id, webhook_event_id)
    WHERE trigger_type = 'WEBHOOK';

ALTER TABLE ai_personal_routine_commands
    DROP CONSTRAINT ck_ai_personal_routine_command_type,
    ADD COLUMN rollback_target_revision INTEGER,
    ADD COLUMN rollback_target_fingerprint CHAR(64);

ALTER TABLE ai_personal_routine_commands
    ADD CONSTRAINT ck_ai_personal_routine_command_type CHECK (
        command_type IN (
            'CREATE', 'UPDATE', 'CONSENT', 'DRY_RUN', 'LIFECYCLE', 'ARCHIVE',
            'ACTIVATION', 'TRIGGER_RUN', 'RUN_COMMAND', 'WEBHOOK_TRIGGER',
            'ROLLBACK'
        )
    ),
    ADD CONSTRAINT ck_ai_personal_routine_command_rollback CHECK (
        (command_type = 'ROLLBACK'
            AND rollback_target_revision > 0
            AND rollback_target_fingerprint ~ '^[0-9a-f]{64}$')
        OR (command_type <> 'ROLLBACK'
            AND rollback_target_revision IS NULL
            AND rollback_target_fingerprint IS NULL)
    );

ALTER TABLE ai_personal_routine_events
    DROP CONSTRAINT ck_ai_personal_routine_event_type;

ALTER TABLE ai_personal_routine_events
    ADD CONSTRAINT ck_ai_personal_routine_event_type CHECK (
        event_type IN (
            'CREATED', 'UPDATED', 'CONSENT_CHANGED', 'DRY_RUN_VALIDATED',
            'LIFECYCLE_CHANGED', 'ARCHIVED', 'ACTIVATED', 'DEACTIVATED',
            'RUN_TRIGGERED', 'RUN_RETRY_REQUESTED', 'RUN_CANCELLED',
            'RUN_COMPENSATION_REQUESTED', 'WEBHOOK_TRIGGERED',
            'EVIDENCE_DOWNLOADED', 'VERSION_ROLLED_BACK'
        )
    );
