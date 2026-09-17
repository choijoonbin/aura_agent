ALTER TABLE ai_artifact_drafts
    ADD COLUMN home_title_envelope TEXT;

ALTER TABLE ai_artifact_drafts
    ADD CONSTRAINT ck_ai_artifact_draft_home_title
    CHECK (home_title_envelope IS NULL OR home_title_envelope LIKE 'dwp2.%');

CREATE TABLE agent_home_identity_assertion_replay (
    jti UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    body_sha256 CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_agent_home_identity_body_sha256
        CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_agent_home_identity_expiry
        CHECK (expires_at > consumed_at)
);

CREATE INDEX idx_agent_home_identity_assertion_expiry
    ON agent_home_identity_assertion_replay (expires_at);

CREATE TABLE agent_artifact_home_projection_backfill_receipts (
    artifact_id UUID PRIMARY KEY REFERENCES ai_artifacts(artifact_id) ON DELETE RESTRICT,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    state VARCHAR(16) NOT NULL,
    safe_error_code VARCHAR(64),
    attempt_count INTEGER NOT NULL DEFAULT 1,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_agent_artifact_home_projection_backfill_state
        CHECK (state IN ('SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_agent_artifact_home_projection_backfill_error
        CHECK ((state = 'SUCCEEDED' AND safe_error_code IS NULL)
            OR (state = 'FAILED' AND safe_error_code IS NOT NULL)),
    CONSTRAINT ck_agent_artifact_home_projection_backfill_attempt
        CHECK (attempt_count BETWEEN 1 AND 5)
);

CREATE INDEX idx_agent_artifact_home_projection_backfill_tenant
    ON agent_artifact_home_projection_backfill_receipts
       (tenant_id, user_id, attempted_at DESC);

COMMENT ON COLUMN ai_artifact_drafts.home_title_envelope IS
    'Separately encrypted least-data title projection for recipient-bound Home reads.';

COMMENT ON TABLE agent_home_identity_assertion_replay IS
    'Single-use admission for short-lived dwp-platform-home delegated identity assertions.';

COMMENT ON TABLE agent_artifact_home_projection_backfill_receipts IS
    'Tenant-bound audit and metric source for bounded lazy Home title projection transition.';
