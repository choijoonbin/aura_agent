CREATE TABLE ai_transactional_outbox_events (
    event_id UUID PRIMARY KEY,
    outbox_id UUID NOT NULL REFERENCES ai_transactional_outbox(outbox_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    event_type VARCHAR(24) NOT NULL,
    generation BIGINT NOT NULL,
    lease_token_fingerprint CHAR(64),
    safe_error_code VARCHAR(128),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_transactional_outbox_event_type CHECK (
        event_type IN ('CLAIMED', 'DELIVERED', 'RETRY_SCHEDULED', 'DEAD_LETTERED')
    ),
    CONSTRAINT ck_ai_transactional_outbox_event_generation CHECK (generation > 0),
    CONSTRAINT ck_ai_transactional_outbox_event_token CHECK (
        lease_token_fingerprint IS NULL OR lease_token_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_transactional_outbox_event_error CHECK (
        safe_error_code IS NULL OR safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
    )
);

CREATE TRIGGER trg_ai_transactional_outbox_events_append_only
BEFORE UPDATE OR DELETE ON ai_transactional_outbox_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

ALTER TABLE ai_personal_routines
    ADD COLUMN source_access_consent_state VARCHAR(32) NOT NULL DEFAULT 'UNSET',
    ADD COLUMN analysis_consent_state VARCHAR(32) NOT NULL DEFAULT 'UNSET',
    ADD COLUMN proposal_delivery_consent_state VARCHAR(32) NOT NULL DEFAULT 'UNSET';

UPDATE ai_personal_routines
   SET source_access_consent_state = consent_state,
       analysis_consent_state = consent_state,
       proposal_delivery_consent_state = consent_state;

ALTER TABLE ai_personal_routines
    DROP CONSTRAINT ck_ai_personal_routine_consent,
    ADD CONSTRAINT ck_ai_personal_routine_consent CHECK (
        consent_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
        AND source_access_consent_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
        AND analysis_consent_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
        AND proposal_delivery_consent_state IN ('UNSET', 'DISABLED', 'ENABLED', 'RECONSENT_REQUIRED')
    );

ALTER TABLE ai_personal_routine_consents
    ADD COLUMN consent_scope VARCHAR(32) NOT NULL DEFAULT 'ALL';

ALTER TABLE ai_personal_routine_consents
    ALTER COLUMN consent_scope DROP DEFAULT,
    DROP CONSTRAINT uq_ai_personal_routine_consent_command,
    ADD CONSTRAINT uq_ai_personal_routine_consent_command
        UNIQUE (tenant_id, user_id, command_id, consent_scope),
    ADD CONSTRAINT ck_ai_personal_routine_consent_scope_key CHECK (
        consent_scope IN ('SOURCE_ACCESS', 'ANALYSIS', 'PROPOSAL_DELIVERY', 'ALL')
    );

ALTER TABLE ai_personal_routine_commands
    DROP CONSTRAINT ck_ai_personal_routine_command_type,
    ADD CONSTRAINT ck_ai_personal_routine_command_type CHECK (
        command_type IN ('CREATE', 'UPDATE', 'CONSENT', 'LIFECYCLE', 'DRY_RUN', 'ARCHIVE')
    );

ALTER TABLE ai_personal_routine_events
    DROP CONSTRAINT ck_ai_personal_routine_event_type,
    ADD CONSTRAINT ck_ai_personal_routine_event_type CHECK (
        event_type IN ('CREATED', 'UPDATED', 'CONSENT_CHANGED', 'LIFECYCLE_CHANGED',
                       'DRY_RUN_VALIDATED', 'ARCHIVED')
    );

CREATE TABLE ai_user_ai_source_preferences (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    source_key VARCHAR(32) NOT NULL,
    enabled BOOLEAN NOT NULL,
    revision INTEGER NOT NULL,
    updated_by_user_id VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id, source_key),
    CONSTRAINT ck_ai_user_source_preference_source CHECK (
        source_key IN ('WORK_ITEM', 'MAIL', 'CALENDAR')
    ),
    CONSTRAINT ck_ai_user_source_preference_revision CHECK (revision > 0)
);

ALTER TABLE ai_user_memory_commands
    DROP CONSTRAINT ck_ai_user_memory_command_type,
    ADD CONSTRAINT ck_ai_user_memory_command_type CHECK (
        command_type IN ('PREFERENCE', 'SOURCE_PREFERENCE', 'CREATE', 'UPDATE', 'STATE', 'DELETE')
    );

ALTER TABLE ai_user_memory_events
    ALTER COLUMN event_type TYPE VARCHAR(32),
    DROP CONSTRAINT ck_ai_user_memory_event_target,
    DROP CONSTRAINT ck_ai_user_memory_event_type,
    ADD CONSTRAINT ck_ai_user_memory_event_target CHECK (
        target_type IN ('PREFERENCE', 'SOURCE', 'MEMORY')
    ),
    ADD CONSTRAINT ck_ai_user_memory_event_type CHECK (
        event_type IN ('PREFERENCE_CHANGED', 'SOURCE_PREFERENCE_CHANGED', 'CREATED',
                       'UPDATED', 'STATE_CHANGED', 'DELETED')
    );

ALTER TABLE ai_artifact_preflight_runs
    ADD COLUMN artifact_revision INTEGER;

ALTER TABLE ai_artifact_preflight_runs
    DISABLE TRIGGER trg_ai_artifact_preflight_runs_append_only;

UPDATE ai_artifact_preflight_runs AS preflight
   SET artifact_revision = event.revision
  FROM ai_artifact_events AS event
 WHERE event.artifact_id = preflight.artifact_id
   AND event.tenant_id = preflight.tenant_id
   AND event.user_id = preflight.user_id
   AND event.command_id = preflight.command_id
   AND event.event_type = 'PREFLIGHT_COMPLETED';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM ai_artifact_preflight_runs
         WHERE artifact_revision IS NULL
    ) THEN
        RAISE EXCEPTION
            'Cannot recover artifact revision for an existing preflight run';
    END IF;
END $$;

ALTER TABLE ai_artifact_preflight_runs
    ALTER COLUMN artifact_revision SET NOT NULL,
    ADD CONSTRAINT ck_ai_artifact_preflight_revision CHECK (artifact_revision > 0);

ALTER TABLE ai_artifact_preflight_runs
    ENABLE TRIGGER trg_ai_artifact_preflight_runs_append_only;

ALTER TABLE ai_artifact_version_sources
    ADD CONSTRAINT ck_ai_artifact_version_source_type CHECK (
        source_type IN ('WORK_ITEM', 'MAIL', 'CALENDAR', 'APPROVAL_TASK',
                        'APPROVAL_REQUEST', 'APPROVAL_FORM', 'APPROVAL_OPERATION')
    );

COMMENT ON TABLE ai_artifact_versions IS
    'Immutable encrypted Artifact checkpoints. V26 provides read and compare, not restore.';

COMMENT ON TABLE ai_transactional_outbox_events IS
    'Append-only lease and delivery evidence. An event is not proof of an external side effect.';

COMMENT ON TABLE ai_user_ai_source_preferences IS
    'Explicit per-user source controls. Disabled sources remain unavailable to personal AI flows.';
