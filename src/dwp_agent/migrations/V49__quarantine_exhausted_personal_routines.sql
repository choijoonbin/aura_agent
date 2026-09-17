ALTER TABLE ai_personal_routine_commands
    DROP CONSTRAINT ck_ai_personal_routine_command_type,
    ADD CONSTRAINT ck_ai_personal_routine_command_type CHECK (
        command_type IN (
            'CREATE', 'UPDATE', 'CONSENT', 'DRY_RUN', 'LIFECYCLE', 'ARCHIVE',
            'ACTIVATION', 'TRIGGER_RUN', 'RUN_COMMAND', 'WEBHOOK_TRIGGER',
            'ROLLBACK', 'AUTO_QUARANTINE'
        )
    );

ALTER TABLE ai_personal_routine_events
    DROP CONSTRAINT ck_ai_personal_routine_event_type,
    ADD CONSTRAINT ck_ai_personal_routine_event_type CHECK (
        event_type IN (
            'CREATED', 'UPDATED', 'CONSENT_CHANGED', 'DRY_RUN_VALIDATED',
            'LIFECYCLE_CHANGED', 'ARCHIVED', 'ACTIVATED', 'DEACTIVATED',
            'RUN_TRIGGERED', 'RUN_RETRY_REQUESTED', 'RUN_CANCELLED',
            'RUN_COMPENSATION_REQUESTED', 'WEBHOOK_TRIGGERED',
            'VERSION_ROLLED_BACK', 'EVIDENCE_DOWNLOADED', 'AUTO_QUARANTINED'
        )
    );

COMMENT ON COLUMN ai_personal_routines.lifecycle_state IS
    'ACTIVE routines are atomically paused when the governed worker exhausts the exact revision retry policy.';
