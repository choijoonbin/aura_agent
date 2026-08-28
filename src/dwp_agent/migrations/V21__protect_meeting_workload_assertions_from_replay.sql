CREATE TABLE agent_meeting_assertion_replay (
    jti UUID PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    tenant_id BIGINT NOT NULL CHECK (tenant_id > 0),
    meeting_id UUID NOT NULL,
    run_id UUID NOT NULL,
    body_sha256 CHAR(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_agent_meeting_assertion_expiry CHECK (expires_at > seen_at)
);

CREATE INDEX ix_agent_meeting_assertion_expiry
    ON agent_meeting_assertion_replay (expires_at);

COMMENT ON TABLE agent_meeting_assertion_replay IS
    'Content-free one-time workload assertion evidence for meeting transcript analysis.';
