CREATE TABLE ai_artifacts (
    artifact_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    artifact_type VARCHAR(24) NOT NULL,
    artifact_state VARCHAR(24) NOT NULL DEFAULT 'DRAFT',
    revision INTEGER NOT NULL DEFAULT 1,
    current_draft_revision INTEGER NOT NULL DEFAULT 1,
    current_version_number INTEGER NOT NULL DEFAULT 0,
    published_version_number INTEGER,
    retention_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TIMESTAMPTZ,
    CONSTRAINT uq_ai_artifact_owner UNIQUE (artifact_id, tenant_id, user_id),
    CONSTRAINT ck_ai_artifact_type CHECK (
        artifact_type IN ('DOCUMENT', 'WORK_PLAN', 'COMPARISON')
    ),
    CONSTRAINT ck_ai_artifact_state CHECK (
        artifact_state IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'ARCHIVED')
    ),
    CONSTRAINT ck_ai_artifact_revisions CHECK (
        revision > 0 AND current_draft_revision > 0 AND current_version_number >= 0
    ),
    CONSTRAINT ck_ai_artifact_published_version CHECK (
        (artifact_state = 'PUBLISHED' AND published_version_number IS NOT NULL
            AND published_version_number > 0)
        OR (artifact_state <> 'PUBLISHED')
    ),
    CONSTRAINT ck_ai_artifact_archive CHECK (
        (artifact_state = 'ARCHIVED' AND archived_at IS NOT NULL)
        OR (artifact_state <> 'ARCHIVED' AND archived_at IS NULL)
    )
);

CREATE INDEX idx_ai_artifacts_owner
    ON ai_artifacts (tenant_id, user_id, updated_at DESC)
    WHERE artifact_state <> 'ARCHIVED';

CREATE TABLE ai_artifact_drafts (
    artifact_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    draft_revision INTEGER NOT NULL,
    base_version_number INTEGER,
    content_envelope TEXT NOT NULL,
    content_fingerprint CHAR(64) NOT NULL,
    updated_by_user_id VARCHAR(160) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_artifact_draft_owner
        FOREIGN KEY (artifact_id, tenant_id, user_id)
        REFERENCES ai_artifacts (artifact_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_artifact_draft_revision CHECK (draft_revision > 0),
    CONSTRAINT ck_ai_artifact_draft_base CHECK (
        base_version_number IS NULL OR base_version_number > 0
    ),
    CONSTRAINT ck_ai_artifact_draft_content CHECK (content_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_artifact_draft_fingerprint CHECK (content_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TABLE ai_artifact_draft_sources (
    source_link_id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL REFERENCES ai_artifact_drafts(artifact_id) ON DELETE RESTRICT,
    source_type VARCHAR(40) NOT NULL,
    reference_fingerprint CHAR(64) NOT NULL,
    reference_envelope TEXT NOT NULL,
    verification_state VARCHAR(16) NOT NULL DEFAULT 'UNVERIFIED',
    CONSTRAINT uq_ai_artifact_draft_source UNIQUE (artifact_id, reference_fingerprint),
    CONSTRAINT ck_ai_artifact_draft_source_type CHECK (
        source_type IN ('WORK_ITEM', 'MAIL', 'CALENDAR', 'APPROVAL_TASK',
                        'APPROVAL_REQUEST', 'APPROVAL_FORM', 'APPROVAL_OPERATION')
    ),
    CONSTRAINT ck_ai_artifact_draft_source_fingerprint CHECK (
        reference_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_draft_source_envelope CHECK (reference_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_artifact_draft_source_state CHECK (verification_state = 'UNVERIFIED')
);

CREATE TABLE ai_artifact_versions (
    artifact_id UUID NOT NULL,
    version_number INTEGER NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    content_envelope TEXT NOT NULL,
    content_fingerprint CHAR(64) NOT NULL,
    source_count INTEGER NOT NULL,
    created_by_user_id VARCHAR(160) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (artifact_id, version_number),
    CONSTRAINT fk_ai_artifact_version_owner
        FOREIGN KEY (artifact_id, tenant_id, user_id)
        REFERENCES ai_artifacts (artifact_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_artifact_version_number CHECK (version_number > 0),
    CONSTRAINT ck_ai_artifact_version_content CHECK (content_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_artifact_version_fingerprint CHECK (content_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_artifact_version_sources CHECK (source_count BETWEEN 0 AND 20)
);

CREATE TRIGGER trg_ai_artifact_versions_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_versions
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_artifact_version_sources (
    source_link_id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL,
    version_number INTEGER NOT NULL,
    source_type VARCHAR(40) NOT NULL,
    reference_fingerprint CHAR(64) NOT NULL,
    reference_envelope TEXT NOT NULL,
    verification_state VARCHAR(16) NOT NULL,
    CONSTRAINT fk_ai_artifact_version_source
        FOREIGN KEY (artifact_id, version_number)
        REFERENCES ai_artifact_versions (artifact_id, version_number)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_version_source
        UNIQUE (artifact_id, version_number, reference_fingerprint),
    CONSTRAINT ck_ai_artifact_version_source_fingerprint CHECK (
        reference_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_version_source_envelope CHECK (reference_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_artifact_version_source_state CHECK (verification_state = 'UNVERIFIED')
);

CREATE TABLE ai_artifact_preflight_runs (
    preflight_id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL,
    version_number INTEGER NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    content_fingerprint CHAR(64) NOT NULL,
    policy_key VARCHAR(64) NOT NULL,
    policy_version INTEGER NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    findings_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT fk_ai_artifact_preflight_version
        FOREIGN KEY (artifact_id, version_number)
        REFERENCES ai_artifact_versions (artifact_id, version_number)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_preflight_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT uq_ai_artifact_preflight_subject UNIQUE (preflight_id, artifact_id, version_number),
    CONSTRAINT ck_ai_artifact_preflight_fingerprint CHECK (content_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_artifact_preflight_policy CHECK (
        policy_key = 'DWP_DETERMINISTIC_DLP_V1' AND policy_version = 1
    ),
    CONSTRAINT ck_ai_artifact_preflight_outcome CHECK (
        outcome IN ('PASS', 'REVIEW', 'BLOCKED')
    ),
    CONSTRAINT ck_ai_artifact_preflight_findings CHECK (findings_envelope LIKE 'dwp2.%'),
    CONSTRAINT ck_ai_artifact_preflight_expiry CHECK (expires_at > created_at)
);

CREATE TRIGGER trg_ai_artifact_preflight_runs_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_preflight_runs
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_artifact_export_jobs (
    export_job_id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL,
    version_number INTEGER NOT NULL,
    preflight_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    export_format VARCHAR(16) NOT NULL,
    export_state VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    generation BIGINT NOT NULL DEFAULT 0,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    output_reference_envelope TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    retention_until TIMESTAMPTZ NOT NULL,
    CONSTRAINT fk_ai_artifact_export_version
        FOREIGN KEY (artifact_id, version_number)
        REFERENCES ai_artifact_versions (artifact_id, version_number)
        ON DELETE RESTRICT,
    CONSTRAINT fk_ai_artifact_export_preflight
        FOREIGN KEY (preflight_id, artifact_id, version_number)
        REFERENCES ai_artifact_preflight_runs (preflight_id, artifact_id, version_number)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_export_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_artifact_export_format CHECK (
        export_format IN ('MARKDOWN', 'DOCX', 'PDF')
    ),
    CONSTRAINT ck_ai_artifact_export_state CHECK (
        export_state IN ('PENDING', 'CLAIMED', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED')
    ),
    CONSTRAINT ck_ai_artifact_export_counts CHECK (generation >= 0 AND attempt_count >= 0),
    CONSTRAINT ck_ai_artifact_export_lease CHECK (
        (export_state = 'CLAIMED' AND generation > 0 AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND completed_at IS NULL)
        OR (export_state <> 'CLAIMED' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_ai_artifact_export_result CHECK (
        output_reference_envelope IS NULL OR output_reference_envelope LIKE 'dwp2.%'
    )
);

CREATE INDEX idx_ai_artifact_export_claim
    ON ai_artifact_export_jobs (export_state, requested_at)
    WHERE export_state IN ('PENDING', 'CLAIMED');

CREATE TABLE ai_artifact_commands (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    artifact_id UUID NOT NULL,
    command_type VARCHAR(24) NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    result_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id, command_id),
    CONSTRAINT fk_ai_artifact_command_owner
        FOREIGN KEY (artifact_id, tenant_id, user_id)
        REFERENCES ai_artifacts (artifact_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_artifact_command_type CHECK (
        command_type IN ('CREATE', 'AUTOSAVE', 'VERSION', 'PREFLIGHT', 'PUBLISH', 'EXPORT')
    ),
    CONSTRAINT ck_ai_artifact_command_session CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_artifact_command_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_artifact_command_result CHECK (result_envelope LIKE 'dwp2.%')
);

CREATE TRIGGER trg_ai_artifact_commands_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_commands
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_artifact_events (
    event_id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(24) NOT NULL,
    previous_state VARCHAR(24),
    current_state VARCHAR(24) NOT NULL,
    revision INTEGER NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    change_reason_envelope TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_artifact_event_owner
        FOREIGN KEY (artifact_id, tenant_id, user_id)
        REFERENCES ai_artifacts (artifact_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_event_command UNIQUE (tenant_id, user_id, command_id),
    CONSTRAINT ck_ai_artifact_event_type CHECK (
        event_type IN ('CREATED', 'AUTOSAVED', 'VERSION_CREATED', 'PREFLIGHT_COMPLETED',
                       'PUBLISHED', 'EXPORT_REQUESTED')
    ),
    CONSTRAINT ck_ai_artifact_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_artifact_event_request CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ai_artifact_event_reason CHECK (reason_code ~ '^[A-Z][A-Z0-9_.-]{1,63}$'),
    CONSTRAINT ck_ai_artifact_event_reason_envelope CHECK (
        change_reason_envelope IS NULL OR change_reason_envelope LIKE 'dwp2.%'
    )
);

CREATE TRIGGER trg_ai_artifact_events_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

COMMENT ON TABLE ai_artifact_drafts IS
    'Mutable encrypted working copy protected by optimistic draft revision.';
COMMENT ON TABLE ai_artifact_versions IS
    'Immutable encrypted Artifact checkpoints; restore creates a new version.';
COMMENT ON TABLE ai_artifact_export_jobs IS
    'A PENDING export is only an internal request and never proof that a file exists.';
