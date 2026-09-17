ALTER TABLE ai_personal_routine_executions
    ADD COLUMN recovery_action VARCHAR(48),
    ADD COLUMN recovery_command_id UUID,
    ADD COLUMN recovery_decision_envelope TEXT,
    ADD CONSTRAINT ck_ai_personal_routine_execution_recovery CHECK (
        (recovery_action IS NULL
            AND recovery_command_id IS NULL
            AND recovery_decision_envelope IS NULL)
        OR (recovery_action = 'SKIP_QUARANTINED_AND_CONTINUE'
            AND recovery_command_id IS NOT NULL
            AND recovery_decision_envelope LIKE 'dwp2.%')
    );

ALTER TABLE ai_personal_routine_events
    DROP CONSTRAINT ck_ai_personal_routine_event_type,
    ADD CONSTRAINT ck_ai_personal_routine_event_type CHECK (
        event_type IN (
            'CREATED', 'UPDATED', 'CONSENT_CHANGED', 'DRY_RUN_VALIDATED',
            'LIFECYCLE_CHANGED', 'ARCHIVED', 'ACTIVATED', 'DEACTIVATED',
            'RUN_TRIGGERED', 'RUN_RETRY_REQUESTED', 'RUN_CANCELLED',
            'RUN_COMPENSATION_REQUESTED', 'RUN_SKIP_QUARANTINED_REQUESTED',
            'WEBHOOK_TRIGGERED', 'VERSION_ROLLED_BACK', 'EVIDENCE_DOWNLOADED',
            'AUTO_QUARANTINED'
        )
    );

COMMENT ON COLUMN ai_personal_routine_executions.recovery_decision_envelope IS
    'Encrypted, audit-bound HITL decision used to continue a partial run while excluding provider-quarantined records.';
