CREATE OR REPLACE FUNCTION reject_ai_audit_event_mutation()
RETURNS TRIGGER AS $$
DECLARE
    disposition_job ai_data_deletion_jobs%ROWTYPE;
    job_id_text TEXT;
    generation_text TEXT;
    lease_token_text TEXT;
    domain_text TEXT;
    old_value JSONB;
BEGIN
    IF TG_OP <> 'DELETE' THEN
        RAISE EXCEPTION 'DWAI-ON audit evidence is append-only';
    END IF;

    job_id_text := current_setting('dwp.disposition_job_id', TRUE);
    generation_text := current_setting('dwp.disposition_generation', TRUE);
    lease_token_text := current_setting('dwp.disposition_lease_token', TRUE);
    domain_text := current_setting('dwp.disposition_domain', TRUE);

    IF job_id_text IS NULL OR generation_text IS NULL
       OR lease_token_text IS NULL OR domain_text IS NULL THEN
        RAISE EXCEPTION 'DWAI-ON audit evidence is append-only';
    END IF;

    SELECT *
      INTO disposition_job
      FROM ai_data_deletion_jobs
     WHERE deletion_job_id = job_id_text::UUID
       AND state = 'RUNNING'
       AND generation = generation_text::BIGINT
       AND lease_token = lease_token_text::UUID
       AND lease_expires_at > CURRENT_TIMESTAMP;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'DWAI-ON audit evidence is append-only';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM ai_data_deletion_targets
         WHERE deletion_job_id = disposition_job.deletion_job_id
           AND domain_key = domain_text
           AND state = 'RUNNING'
    ) THEN
        RAISE EXCEPTION 'DWAI-ON audit evidence is append-only';
    END IF;

    IF NOT (
        (domain_text = 'MEMORY' AND TG_TABLE_NAME IN (
            'ai_user_memory_commands', 'ai_user_memory_events'
        ))
        OR (domain_text = 'ROUTINE' AND TG_TABLE_NAME IN (
            'ai_personal_routine_commands', 'ai_personal_routine_consents',
            'ai_personal_routine_runs', 'ai_personal_routine_events'
        ))
        OR (domain_text = 'ARTIFACT' AND TG_TABLE_NAME IN (
            'ai_artifact_versions', 'ai_artifact_version_sources',
            'ai_artifact_preflight_runs', 'ai_artifact_commands',
            'ai_artifact_events'
        ))
        OR (domain_text = 'ARTIFACT_EXPORT' AND TG_TABLE_NAME IN (
            'ai_artifact_export_outputs', 'ai_artifact_export_events',
            'ai_artifact_commands', 'ai_artifact_events',
            'ai_transactional_outbox_events'
        ))
    ) THEN
        RAISE EXCEPTION 'DWAI-ON audit evidence is append-only';
    END IF;

    old_value := to_jsonb(OLD);
    IF old_value ? 'tenant_id'
       AND (old_value ->> 'tenant_id')::BIGINT <> disposition_job.tenant_id THEN
        RAISE EXCEPTION 'Disposition tenant boundary mismatch';
    END IF;
    IF old_value ? 'user_id'
       AND old_value ->> 'user_id' <> disposition_job.user_id THEN
        RAISE EXCEPTION 'Disposition owner boundary mismatch';
    END IF;

    IF TG_TABLE_NAME = 'ai_artifact_version_sources' AND NOT EXISTS (
        SELECT 1
          FROM ai_artifacts AS artifact
         WHERE artifact.artifact_id = (old_value ->> 'artifact_id')::UUID
           AND artifact.tenant_id = disposition_job.tenant_id
           AND artifact.user_id = disposition_job.user_id
    ) THEN
        RAISE EXCEPTION 'Disposition artifact source owner boundary mismatch';
    END IF;

    IF TG_TABLE_NAME = 'ai_transactional_outbox_events' AND NOT EXISTS (
        SELECT 1
          FROM ai_transactional_outbox AS outbox
         WHERE outbox.outbox_id = (old_value ->> 'outbox_id')::UUID
           AND outbox.tenant_id = disposition_job.tenant_id
           AND outbox.user_id = disposition_job.user_id
    ) THEN
        RAISE EXCEPTION 'Disposition outbox owner boundary mismatch';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION reject_ai_audit_event_mutation() IS
    'Append-only guard. Domain-scoped deletion is allowed only inside a live, fenced personal-data disposition lease; updates remain forbidden.';
