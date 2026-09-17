ALTER TABLE ai_artifacts
    ADD COLUMN tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN project_key VARCHAR(80),
    ADD COLUMN review_sla_due_at TIMESTAMPTZ,
    ADD CONSTRAINT ck_ai_artifact_tags CHECK (cardinality(tags) <= 20),
    ADD CONSTRAINT ck_ai_artifact_project_key CHECK (
        project_key IS NULL OR project_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$'
    ),
    ADD CONSTRAINT ck_ai_artifact_review_sla CHECK (
        review_sla_due_at IS NULL OR review_sla_due_at > created_at
    );

CREATE INDEX idx_ai_artifacts_project
    ON ai_artifacts (tenant_id, project_key, updated_at DESC)
    WHERE project_key IS NOT NULL;

CREATE INDEX idx_ai_artifacts_review_sla
    ON ai_artifacts (tenant_id, review_sla_due_at)
    WHERE review_sla_due_at IS NOT NULL AND artifact_state = 'REVIEW_REQUIRED';

ALTER TABLE ai_artifact_team_workspaces
    ADD CONSTRAINT uq_ai_artifact_team_workspace_review_boundary
        UNIQUE (workspace_id, artifact_id, tenant_id);

ALTER TABLE ai_artifact_collaboration_commands
    DROP CONSTRAINT ck_ai_artifact_collaboration_command_type;

ALTER TABLE ai_artifact_collaboration_commands
    ADD CONSTRAINT ck_ai_artifact_collaboration_command_type CHECK (
        command_type IN (
            'PREFLIGHT', 'CREATE_WORKSPACE', 'UPDATE_MEMBERS', 'EDIT',
            'RESOLVE_CONFLICT', 'CREATE_SHARE', 'REVOKE_SHARE',
            'REQUEST_ACCESS', 'CREATE_COMMENT', 'REPLY_COMMENT',
            'RESOLVE_COMMENT', 'REVIEW_DECISION'
        )
    );

ALTER TABLE ai_artifact_collaboration_events
    DROP CONSTRAINT ck_ai_artifact_collaboration_event_type;

ALTER TABLE ai_artifact_collaboration_events
    ADD CONSTRAINT ck_ai_artifact_collaboration_event_type CHECK (
        event_type IN (
            'PREFLIGHT_COMPLETED', 'WORKSPACE_CREATED', 'MEMBERS_UPDATED',
            'EDIT_APPLIED', 'CONFLICT_DETECTED', 'CONFLICT_RESOLVED',
            'SHARE_CREATED', 'SHARE_REVOKED', 'ACCESS_REQUESTED',
            'COMMENT_CREATED', 'COMMENT_REPLIED', 'COMMENT_RESOLVED',
            'REVIEW_APPROVED', 'REVIEW_REJECTED'
        )
    );

CREATE TABLE ai_artifact_review_stages (
    stage_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    artifact_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    stage_order INTEGER NOT NULL,
    stage_key VARCHAR(32) NOT NULL,
    assignee_subject_id VARCHAR(160),
    stage_state VARCHAR(24) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    evidence_fingerprint CHAR(64),
    decided_by_user_id VARCHAR(160),
    decision_reason_envelope TEXT,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ai_artifact_review_stage_order UNIQUE (workspace_id, stage_order),
    CONSTRAINT uq_ai_artifact_review_stage_key UNIQUE (workspace_id, stage_key),
    CONSTRAINT fk_ai_artifact_review_stage_boundary
        FOREIGN KEY (workspace_id, artifact_id, tenant_id)
        REFERENCES ai_artifact_team_workspaces(workspace_id, artifact_id, tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_artifact_review_stage_order CHECK (stage_order BETWEEN 1 AND 3),
    CONSTRAINT ck_ai_artifact_review_stage_key CHECK (
        stage_key IN ('AUTHOR', 'PRIMARY_REVIEW', 'FINAL_APPROVAL')
    ),
    CONSTRAINT ck_ai_artifact_review_stage_state CHECK (
        stage_state IN ('PENDING', 'APPROVED', 'REJECTED', 'UNAVAILABLE')
    ),
    CONSTRAINT ck_ai_artifact_review_stage_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_artifact_review_stage_evidence CHECK (
        evidence_fingerprint IS NULL OR evidence_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_review_stage_decision CHECK (
        (stage_state IN ('APPROVED', 'REJECTED')
            AND assignee_subject_id IS NOT NULL
            AND evidence_fingerprint IS NOT NULL
            AND evidence_fingerprint ~ '^[0-9a-f]{64}$'
            AND decided_by_user_id IS NOT NULL
            AND decision_reason_envelope IS NOT NULL
            AND decision_reason_envelope LIKE 'dwp2.%'
            AND decided_at IS NOT NULL)
        OR (stage_state = 'PENDING'
            AND assignee_subject_id IS NOT NULL
            AND evidence_fingerprint IS NULL
            AND decided_by_user_id IS NULL
            AND decision_reason_envelope IS NULL
            AND decided_at IS NULL)
        OR (stage_state = 'UNAVAILABLE'
            AND assignee_subject_id IS NULL
            AND evidence_fingerprint IS NULL
            AND decided_by_user_id IS NULL
            AND decision_reason_envelope IS NULL
            AND decided_at IS NULL)
    )
);

CREATE FUNCTION guard_ai_artifact_review_stage_update()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.stage_id IS DISTINCT FROM OLD.stage_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.stage_order IS DISTINCT FROM OLD.stage_order
       OR NEW.stage_key IS DISTINCT FROM OLD.stage_key
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.revision <> OLD.revision + 1
       OR OLD.stage_state <> 'PENDING'
       OR NEW.stage_state NOT IN ('APPROVED', 'REJECTED')
       OR NEW.updated_at < OLD.updated_at
    THEN
        RAISE EXCEPTION 'artifact review stages only support governed pending decisions';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ai_artifact_review_stages_govern_update
BEFORE UPDATE ON ai_artifact_review_stages
FOR EACH ROW EXECUTE FUNCTION guard_ai_artifact_review_stage_update();

CREATE TRIGGER trg_ai_artifact_review_stages_reject_delete
BEFORE DELETE ON ai_artifact_review_stages
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE INDEX idx_ai_artifact_review_stages_assignee
    ON ai_artifact_review_stages (tenant_id, assignee_subject_id, stage_state, updated_at DESC)
    WHERE stage_state = 'PENDING';

COMMENT ON TABLE ai_artifact_review_stages IS
    'Tenant-scoped staged artifact review decisions with optimistic revisions and encrypted reasons.';
