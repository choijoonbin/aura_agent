CREATE TABLE ai_artifact_team_preflights (
    preflight_id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL,
    team_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    owner_user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    artifact_revision INTEGER NOT NULL,
    preflight_state VARCHAR(32) NOT NULL,
    decision_revision INTEGER NOT NULL,
    members_envelope TEXT NOT NULL,
    sources_envelope TEXT NOT NULL,
    allowed_source_count INTEGER NOT NULL,
    excluded_source_count INTEGER NOT NULL,
    evidence_sha256 CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_artifact_team_preflight_owner
        FOREIGN KEY (artifact_id, tenant_id, owner_user_id)
        REFERENCES ai_artifacts (artifact_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_team_preflight_command
        UNIQUE (tenant_id, owner_user_id, command_id),
    CONSTRAINT ck_ai_artifact_team_preflight_state CHECK (
        preflight_state IN ('READY', 'PARTIAL', 'PERMISSION_DENIED')
    ),
    CONSTRAINT ck_ai_artifact_team_preflight_revision CHECK (
        artifact_revision > 0 AND decision_revision > 0
    ),
    CONSTRAINT ck_ai_artifact_team_preflight_envelopes CHECK (
        members_envelope LIKE 'dwp2.%' AND sources_envelope LIKE 'dwp2.%'
    ),
    CONSTRAINT ck_ai_artifact_team_preflight_counts CHECK (
        allowed_source_count BETWEEN 0 AND 20
        AND excluded_source_count BETWEEN 0 AND 20
    ),
    CONSTRAINT ck_ai_artifact_team_preflight_evidence CHECK (
        evidence_sha256 ~ '^[0-9a-f]{64}$'
        AND request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_team_preflight_expiry CHECK (
        expires_at > created_at
    )
);

CREATE TABLE ai_artifact_team_workspaces (
    workspace_id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL,
    team_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    owner_user_id VARCHAR(160) NOT NULL,
    workspace_state VARCHAR(24) NOT NULL DEFAULT 'ACTIVE',
    revision INTEGER NOT NULL DEFAULT 1,
    source_artifact_revision INTEGER NOT NULL,
    content_envelope TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT uq_ai_artifact_team_workspace_artifact UNIQUE (artifact_id),
    CONSTRAINT uq_ai_artifact_team_workspace_owner
        UNIQUE (workspace_id, tenant_id, owner_user_id),
    CONSTRAINT fk_ai_artifact_team_workspace_owner
        FOREIGN KEY (artifact_id, tenant_id, owner_user_id)
        REFERENCES ai_artifacts (artifact_id, tenant_id, user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_artifact_team_workspace_state CHECK (
        workspace_state IN ('ACTIVE', 'READ_ONLY', 'REVOKED')
    ),
    CONSTRAINT ck_ai_artifact_team_workspace_revision CHECK (
        revision > 0 AND source_artifact_revision > 0
    ),
    CONSTRAINT ck_ai_artifact_team_workspace_content CHECK (
        content_envelope LIKE 'dwp2.%' AND content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_team_workspace_revoke CHECK (
        (workspace_state = 'REVOKED' AND revoked_at IS NOT NULL)
        OR (workspace_state <> 'REVOKED' AND revoked_at IS NULL)
    )
);

CREATE TABLE ai_artifact_team_members (
    workspace_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    subject_id VARCHAR(160) NOT NULL,
    member_role VARCHAR(24) NOT NULL,
    allowed BOOLEAN NOT NULL,
    denied_source_count INTEGER NOT NULL DEFAULT 0,
    reason_code VARCHAR(128),
    decision_revision INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, subject_id),
    CONSTRAINT fk_ai_artifact_team_member_workspace
        FOREIGN KEY (workspace_id) REFERENCES ai_artifact_team_workspaces(workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_artifact_team_member_role CHECK (
        member_role IN ('OWNER', 'EDITOR', 'REVIEWER', 'VIEWER')
    ),
    CONSTRAINT ck_ai_artifact_team_member_decision CHECK (
        denied_source_count >= 0 AND decision_revision > 0
        AND (reason_code IS NULL OR reason_code ~ '^[A-Z][A-Z0-9_.-]{1,127}$')
    )
);

CREATE TABLE ai_artifact_team_versions (
    workspace_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    tenant_id BIGINT NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    content_envelope TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, revision),
    CONSTRAINT fk_ai_artifact_team_version_workspace
        FOREIGN KEY (workspace_id) REFERENCES ai_artifact_team_workspaces(workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_artifact_team_version_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_artifact_team_version_content CHECK (
        content_envelope LIKE 'dwp2.%' AND content_sha256 ~ '^[0-9a-f]{64}$'
    )
);

CREATE TRIGGER trg_ai_artifact_team_versions_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_team_versions
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_artifact_team_conflicts (
    conflict_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    base_revision INTEGER NOT NULL,
    server_revision INTEGER NOT NULL,
    conflict_state VARCHAR(32) NOT NULL DEFAULT 'OPEN',
    local_content_envelope TEXT NOT NULL,
    local_sha256 CHAR(64) NOT NULL,
    server_content_envelope TEXT NOT NULL,
    server_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ,
    CONSTRAINT fk_ai_artifact_team_conflict_workspace
        FOREIGN KEY (workspace_id) REFERENCES ai_artifact_team_workspaces(workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_team_conflict_command
        UNIQUE (tenant_id, actor_user_id, command_id),
    CONSTRAINT ck_ai_artifact_team_conflict_revision CHECK (
        base_revision > 0 AND server_revision > 0
    ),
    CONSTRAINT ck_ai_artifact_team_conflict_state CHECK (
        conflict_state IN (
            'OPEN', 'RESOLVED_LOCAL', 'RESOLVED_SERVER', 'RESOLVED_MERGED',
            'STASHED', 'ROLLED_BACK'
        )
    ),
    CONSTRAINT ck_ai_artifact_team_conflict_content CHECK (
        local_content_envelope LIKE 'dwp2.%'
        AND server_content_envelope LIKE 'dwp2.%'
        AND local_sha256 ~ '^[0-9a-f]{64}$'
        AND server_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_team_conflict_resolved CHECK (
        (conflict_state = 'OPEN' AND resolved_at IS NULL)
        OR (conflict_state <> 'OPEN' AND resolved_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_ai_artifact_team_open_conflict
    ON ai_artifact_team_conflicts (workspace_id)
    WHERE conflict_state = 'OPEN';

CREATE TABLE ai_artifact_team_shares (
    share_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    owner_user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    preflight_id UUID NOT NULL,
    share_state VARCHAR(24) NOT NULL DEFAULT 'ACTIVE',
    permission VARCHAR(16) NOT NULL,
    member_count INTEGER NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    receipt_id UUID NOT NULL,
    receipt_sha256 CHAR(64) NOT NULL,
    revocation_receipt_id UUID,
    revocation_receipt_sha256 CHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT fk_ai_artifact_team_share_workspace
        FOREIGN KEY (workspace_id) REFERENCES ai_artifact_team_workspaces(workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_ai_artifact_team_share_preflight
        FOREIGN KEY (preflight_id) REFERENCES ai_artifact_team_preflights(preflight_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_ai_artifact_team_share_command
        UNIQUE (tenant_id, owner_user_id, command_id),
    CONSTRAINT ck_ai_artifact_team_share_state CHECK (
        share_state IN ('ACTIVE', 'REVOKED')
    ),
    CONSTRAINT ck_ai_artifact_team_share_permission CHECK (
        permission IN ('VIEW', 'COMMENT', 'EDIT')
    ),
    CONSTRAINT ck_ai_artifact_team_share_members CHECK (
        member_count BETWEEN 1 AND 100
    ),
    CONSTRAINT ck_ai_artifact_team_share_receipt CHECK (
        receipt_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ai_artifact_team_share_expiry CHECK (
        expires_at > created_at
    ),
    CONSTRAINT ck_ai_artifact_team_share_revoke CHECK (
        (share_state = 'REVOKED' AND revoked_at IS NOT NULL
            AND revocation_receipt_id IS NOT NULL
            AND revocation_receipt_sha256 ~ '^[0-9a-f]{64}$')
        OR (share_state = 'ACTIVE' AND revoked_at IS NULL
            AND revocation_receipt_id IS NULL
            AND revocation_receipt_sha256 IS NULL)
    )
);

CREATE TABLE ai_artifact_collaboration_commands (
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    artifact_id UUID NOT NULL,
    command_type VARCHAR(32) NOT NULL,
    session_fingerprint CHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    result_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, user_id, command_id),
    CONSTRAINT fk_ai_artifact_collaboration_command_artifact
        FOREIGN KEY (artifact_id)
        REFERENCES ai_artifacts (artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_ai_artifact_collaboration_command_type CHECK (
        command_type IN (
            'PREFLIGHT', 'CREATE_WORKSPACE', 'UPDATE_MEMBERS', 'EDIT',
            'RESOLVE_CONFLICT', 'CREATE_SHARE', 'REVOKE_SHARE'
        )
    ),
    CONSTRAINT ck_ai_artifact_collaboration_command_fingerprints CHECK (
        session_fingerprint ~ '^[0-9a-f]{64}$'
        AND request_fingerprint ~ '^[0-9a-f]{64}$'
        AND result_envelope LIKE 'dwp2.%'
    )
);

CREATE TRIGGER trg_ai_artifact_collaboration_commands_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_collaboration_commands
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE TABLE ai_artifact_collaboration_events (
    event_id UUID PRIMARY KEY,
    workspace_id UUID,
    artifact_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    actor_user_id VARCHAR(160) NOT NULL,
    command_id UUID NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    previous_state VARCHAR(32),
    current_state VARCHAR(32) NOT NULL,
    revision INTEGER NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ai_artifact_collaboration_event_type CHECK (
        event_type IN (
            'PREFLIGHT_COMPLETED', 'WORKSPACE_CREATED', 'MEMBERS_UPDATED',
            'EDIT_APPLIED', 'CONFLICT_DETECTED', 'CONFLICT_RESOLVED',
            'SHARE_CREATED', 'SHARE_REVOKED'
        )
    ),
    CONSTRAINT ck_ai_artifact_collaboration_event_revision CHECK (revision > 0),
    CONSTRAINT ck_ai_artifact_collaboration_event_request CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    )
);

CREATE TRIGGER trg_ai_artifact_collaboration_events_append_only
BEFORE UPDATE OR DELETE ON ai_artifact_collaboration_events
FOR EACH ROW EXECUTE FUNCTION reject_ai_audit_event_mutation();

CREATE INDEX idx_ai_artifact_team_workspaces_team
    ON ai_artifact_team_workspaces (tenant_id, team_id, updated_at DESC)
    WHERE workspace_state <> 'REVOKED';
CREATE INDEX idx_ai_artifact_team_shares_expiry
    ON ai_artifact_team_shares (expires_at)
    WHERE share_state = 'ACTIVE';

COMMENT ON TABLE ai_artifact_team_workspaces IS
    'Governed team artifact workspace with optimistic revisions and encrypted content.';
COMMENT ON TABLE ai_artifact_team_preflights IS
    'Server-verified recipient and source ACL evidence; UI claims cannot authorize sharing.';
