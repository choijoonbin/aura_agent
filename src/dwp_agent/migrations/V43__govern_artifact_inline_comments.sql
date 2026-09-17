ALTER TABLE ai_artifact_collaboration_commands
    DROP CONSTRAINT ck_ai_artifact_collaboration_command_type;

ALTER TABLE ai_artifact_collaboration_commands
    ADD CONSTRAINT ck_ai_artifact_collaboration_command_type CHECK (
        command_type IN (
            'PREFLIGHT', 'CREATE_WORKSPACE', 'UPDATE_MEMBERS', 'EDIT',
            'RESOLVE_CONFLICT', 'CREATE_SHARE', 'REVOKE_SHARE',
            'REQUEST_ACCESS', 'CREATE_COMMENT', 'REPLY_COMMENT',
            'RESOLVE_COMMENT'
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
            'COMMENT_CREATED', 'COMMENT_REPLIED', 'COMMENT_RESOLVED'
        )
    );

CREATE TABLE ai_artifact_team_comments (
    comment_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    artifact_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    author_user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    comment_state VARCHAR(24) NOT NULL DEFAULT 'OPEN',
    revision INTEGER NOT NULL DEFAULT 1,
    body_envelope TEXT NOT NULL,
    anchor_envelope TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ,
    CONSTRAINT fk_ai_artifact_team_comment_workspace
        FOREIGN KEY (workspace_id) REFERENCES ai_artifact_team_workspaces(workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_ai_artifact_team_comment_artifact
        FOREIGN KEY (artifact_id) REFERENCES ai_artifacts(artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_team_comment_identity
        UNIQUE (comment_id, workspace_id, artifact_id, tenant_id),
    CONSTRAINT uq_ai_artifact_team_comment_command
        UNIQUE (tenant_id, author_user_id, command_id),
    CONSTRAINT ck_ai_artifact_team_comment_state CHECK (
        comment_state IN ('OPEN', 'RESOLVED')
    ),
    CONSTRAINT ck_ai_artifact_team_comment_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_artifact_team_comment_content CHECK (
        body_envelope LIKE 'dwp2.%'
        AND (anchor_envelope IS NULL OR anchor_envelope LIKE 'dwp2.%')
    ),
    CONSTRAINT ck_ai_artifact_team_comment_resolution CHECK (
        (comment_state = 'OPEN' AND resolved_at IS NULL)
        OR (comment_state = 'RESOLVED' AND resolved_at IS NOT NULL)
    )
);

CREATE TABLE ai_artifact_team_comment_replies (
    reply_id UUID PRIMARY KEY,
    comment_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    artifact_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    author_user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    body_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_artifact_team_comment_reply_comment
        FOREIGN KEY (comment_id, workspace_id, artifact_id, tenant_id)
        REFERENCES ai_artifact_team_comments(
            comment_id, workspace_id, artifact_id, tenant_id
        ) ON DELETE RESTRICT,
    CONSTRAINT fk_ai_artifact_team_comment_reply_workspace
        FOREIGN KEY (workspace_id) REFERENCES ai_artifact_team_workspaces(workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_team_comment_reply_command
        UNIQUE (tenant_id, author_user_id, command_id),
    CONSTRAINT ck_ai_artifact_team_comment_reply_content CHECK (
        body_envelope LIKE 'dwp2.%'
    )
);

CREATE FUNCTION guard_ai_artifact_team_comment_update()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.comment_state <> 'OPEN'
       OR NEW.comment_state NOT IN ('OPEN', 'RESOLVED')
       OR NEW.revision <> OLD.revision + 1
       OR (NEW.comment_state = 'OPEN' AND NEW.resolved_at IS NOT NULL)
       OR (NEW.comment_state = 'RESOLVED' AND NEW.resolved_at IS NULL)
       OR NEW.updated_at < OLD.updated_at
       OR NEW.comment_id IS DISTINCT FROM OLD.comment_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.author_user_id IS DISTINCT FROM OLD.author_user_id
       OR NEW.command_id IS DISTINCT FROM OLD.command_id
       OR NEW.body_envelope IS DISTINCT FROM OLD.body_envelope
       OR NEW.anchor_envelope IS DISTINCT FROM OLD.anchor_envelope
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'artifact comments only support replies or OPEN to RESOLVED transitions';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ai_artifact_team_comments_govern_update
BEFORE UPDATE ON ai_artifact_team_comments
FOR EACH ROW EXECUTE FUNCTION guard_ai_artifact_team_comment_update();

CREATE TRIGGER trg_ai_artifact_team_comments_reject_delete
BEFORE DELETE ON ai_artifact_team_comments
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TRIGGER trg_ai_artifact_team_comment_replies_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_team_comment_replies
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE INDEX idx_ai_artifact_team_comments_workspace
    ON ai_artifact_team_comments (workspace_id, comment_state, updated_at DESC);

CREATE INDEX idx_ai_artifact_team_comment_replies_comment
    ON ai_artifact_team_comment_replies (comment_id, created_at ASC);

COMMENT ON TABLE ai_artifact_team_comments IS
    'Encrypted, tenant-scoped inline collaboration comments with optimistic resolution.';
COMMENT ON TABLE ai_artifact_team_comment_replies IS
    'Append-only encrypted replies to governed artifact comments.';
