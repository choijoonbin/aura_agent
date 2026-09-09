ALTER TABLE ai_user_memory_preferences
    ADD COLUMN runtime_application_state VARCHAR(16) NOT NULL DEFAULT 'UNSET',
    ADD CONSTRAINT ck_ai_user_memory_runtime_application_state CHECK (
        runtime_application_state IN ('UNSET', 'DISABLED', 'ENABLED')
    );

ALTER TABLE ai_user_memory_commands
    DROP CONSTRAINT ck_ai_user_memory_command_type,
    ADD CONSTRAINT ck_ai_user_memory_command_type CHECK (
        command_type IN ('PREFERENCE', 'RUNTIME_PREFERENCE', 'SOURCE_PREFERENCE',
                         'CREATE', 'UPDATE', 'STATE', 'DELETE')
    );

ALTER TABLE ai_user_memory_events
    DROP CONSTRAINT ck_ai_user_memory_event_type,
    ADD CONSTRAINT ck_ai_user_memory_event_type CHECK (
        event_type IN ('PREFERENCE_CHANGED', 'RUNTIME_APPLICATION_CHANGED',
                       'SOURCE_PREFERENCE_CHANGED', 'CREATED', 'UPDATED',
                       'STATE_CHANGED', 'DELETED')
    );

COMMENT ON COLUMN ai_user_memory_preferences.runtime_application_state IS
    'Separate explicit consent to send active personal presentation preferences to the answer model. Existing users remain UNSET.';
