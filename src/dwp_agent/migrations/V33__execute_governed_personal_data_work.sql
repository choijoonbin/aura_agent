CREATE OR REPLACE FUNCTION reject_ai_audit_event_mutation()
RETURNS TRIGGER AS $$
DECLARE
    disposition_job ai_data_deletion_jobs%ROWTYPE;
    job_id_text TEXT;
    generation_text TEXT;
    lease_token_text TEXT;
    old_value JSONB;
BEGIN
    job_id_text := current_setting('dwp.disposition_job_id', TRUE);
    generation_text := current_setting('dwp.disposition_generation', TRUE);
    lease_token_text := current_setting('dwp.disposition_lease_token', TRUE);

    IF job_id_text IS NULL OR generation_text IS NULL OR lease_token_text IS NULL THEN
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

    old_value := to_jsonb(OLD);
    IF old_value ? 'tenant_id'
       AND (old_value ->> 'tenant_id')::BIGINT <> disposition_job.tenant_id THEN
        RAISE EXCEPTION 'Disposition tenant boundary mismatch';
    END IF;
    IF old_value ? 'user_id'
       AND old_value ->> 'user_id' <> disposition_job.user_id THEN
        RAISE EXCEPTION 'Disposition owner boundary mismatch';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

ALTER TABLE ai_artifact_draft_sources
    DROP CONSTRAINT ck_ai_artifact_draft_source_state,
    ADD COLUMN verified_at TIMESTAMPTZ,
    ADD COLUMN verification_evidence_fingerprint CHAR(64),
    ADD CONSTRAINT ck_ai_artifact_draft_source_state CHECK (
        verification_state IN ('UNVERIFIED', 'SERVER_VERIFIED')
        AND (
            (verification_state = 'UNVERIFIED'
                AND verified_at IS NULL
                AND verification_evidence_fingerprint IS NULL)
            OR (verification_state = 'SERVER_VERIFIED'
                AND verified_at IS NOT NULL
                AND verification_evidence_fingerprint ~ '^[0-9a-f]{64}$')
        )
    );

ALTER TABLE ai_artifact_version_sources
    DROP CONSTRAINT ck_ai_artifact_version_source_state,
    ADD COLUMN verified_at TIMESTAMPTZ,
    ADD COLUMN verification_evidence_fingerprint CHAR(64),
    ADD CONSTRAINT ck_ai_artifact_version_source_state CHECK (
        verification_state IN ('UNVERIFIED', 'SERVER_VERIFIED')
        AND (
            (verification_state = 'UNVERIFIED'
                AND verified_at IS NULL
                AND verification_evidence_fingerprint IS NULL)
            OR (verification_state = 'SERVER_VERIFIED'
                AND verified_at IS NOT NULL
                AND verification_evidence_fingerprint ~ '^[0-9a-f]{64}$')
        )
    );

ALTER TABLE ai_artifact_export_jobs
    ADD COLUMN artifact_revision INTEGER,
    ADD COLUMN safe_error_code VARCHAR(128);

UPDATE ai_artifact_export_jobs AS export
   SET artifact_revision = artifact.revision
  FROM ai_artifacts AS artifact
 WHERE artifact.artifact_id = export.artifact_id;

ALTER TABLE ai_artifact_export_jobs
    ALTER COLUMN artifact_revision SET NOT NULL,
    ADD CONSTRAINT ck_ai_artifact_export_revision CHECK (artifact_revision > 0),
    ADD CONSTRAINT ck_ai_artifact_export_error CHECK (
        safe_error_code IS NULL OR safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
    );

CREATE TABLE ai_artifact_export_outputs (
    export_job_id UUID PRIMARY KEY
        REFERENCES ai_artifact_export_jobs(export_job_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    media_type VARCHAR(96) NOT NULL,
    file_name VARCHAR(240) NOT NULL,
    byte_size BIGINT NOT NULL,
    content_fingerprint CHAR(64) NOT NULL,
    content_envelope TEXT NOT NULL,
    manifest_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retention_until TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_ai_artifact_export_output_size CHECK (byte_size > 0),
    CONSTRAINT ck_ai_artifact_export_output_fingerprint CHECK (
        content_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_export_output_content CHECK (content_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_artifact_export_output_manifest CHECK (manifest_envelope LIKE 'dwp2.%')
);

CREATE TRIGGER trg_ai_artifact_export_outputs_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_export_outputs
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_artifact_export_events (
    event_id UUID PRIMARY KEY,
    export_job_id UUID NOT NULL
        REFERENCES ai_artifact_export_jobs(export_job_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    event_type VARCHAR(24) NOT NULL,
    previous_state VARCHAR(24),
    current_state VARCHAR(24) NOT NULL,
    generation BIGINT NOT NULL,
    content_fingerprint CHAR(64),
    safe_error_code VARCHAR(128),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_artifact_export_event_type CHECK (
        event_type IN ('CLAIMED', 'SUCCEEDED', 'RETRY_SCHEDULED', 'FAILED')
    ),
    CONSTRAINT ck_ai_artifact_export_event_generation CHECK (generation > 0),
    CONSTRAINT ck_ai_artifact_export_event_fingerprint CHECK (
        content_fingerprint IS NULL OR content_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_export_event_error CHECK (
        safe_error_code IS NULL OR safe_error_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$'
    )
);

CREATE TRIGGER trg_ai_artifact_export_events_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_export_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

ALTER TABLE ai_data_deletion_targets
    ADD COLUMN disposition_id UUID,
    ADD COLUMN completed_at TIMESTAMPTZ;

CREATE TABLE ai_data_disposition_receipts (
    disposition_id UUID PRIMARY KEY,
    deletion_job_id UUID NOT NULL
        REFERENCES ai_data_deletion_jobs(deletion_job_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    domain_key VARCHAR(32) NOT NULL,
    generation BIGINT NOT NULL,
    purged_row_count BIGINT NOT NULL,
    table_counts JSONB NOT NULL,
    active_store_envelopes_destroyed BOOLEAN NOT NULL,
    source_system_data_affected BOOLEAN NOT NULL DEFAULT FALSE,
    backup_disposition_state VARCHAR(40) NOT NULL DEFAULT 'EXTERNAL_RETENTION_BOUNDARY',
    receipt_fingerprint CHAR(64) NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_data_disposition_target UNIQUE (deletion_job_id, domain_key),
    CONSTRAINT ck_ai_data_disposition_domain CHECK (
        domain_key IN ('ROUTINE', 'MEMORY', 'ARTIFACT', 'ARTIFACT_EXPORT')
    ),
    CONSTRAINT ck_ai_data_disposition_generation CHECK (generation > 0),
    CONSTRAINT ck_ai_data_disposition_count CHECK (purged_row_count >= 0),
    CONSTRAINT ck_ai_data_disposition_backup CHECK (
        backup_disposition_state = 'EXTERNAL_RETENTION_BOUNDARY'
    ),
    CONSTRAINT ck_ai_data_disposition_receipt CHECK (
        receipt_fingerprint ~ '^[0-9a-f]{64}$'
    )
);

CREATE TRIGGER trg_ai_data_disposition_receipts_append_only
BEFORE UPDATE OR DELETE ON ai_data_disposition_receipts
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

ALTER TABLE ai_data_deletion_targets
    ADD CONSTRAINT fk_ai_data_deletion_target_disposition
        FOREIGN KEY (disposition_id)
        REFERENCES ai_data_disposition_receipts(disposition_id) ON DELETE RESTRICT,
    ADD CONSTRAINT ck_ai_data_deletion_target_completion CHECK (
        (state = 'COMPLETED' AND disposition_id IS NOT NULL AND completed_at IS NOT NULL)
        OR (state <> 'COMPLETED' AND disposition_id IS NULL AND completed_at IS NULL)
    );

COMMENT ON FUNCTION reject_ai_audit_event_mutation() IS
    'Append-only guard. Deletion is allowed only inside a live, fenced personal-data disposition lease.';
COMMENT ON TABLE ai_artifact_export_outputs IS
    'Encrypted generated export bytes and manifest. A SUCCEEDED job must reference one immutable output.';
COMMENT ON TABLE ai_data_disposition_receipts IS
    'Content-free proof of active-store disposal. Backup expiry remains an external retention boundary.';
