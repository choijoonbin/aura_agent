CREATE TABLE agent_meeting_media_assertion_replay (
    purpose VARCHAR(16) NOT NULL
        CHECK (purpose IN ('RECORDING', 'TRANSCRIPT')),
    assertion_scope VARCHAR(16) NOT NULL
        CHECK (assertion_scope IN ('SERVICE', 'RESOURCE')),
    jti UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    tenant_id BIGINT CHECK (tenant_id > 0),
    meeting_id UUID,
    resource_id UUID,
    body_sha256 CHAR(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (purpose, jti),
    CONSTRAINT ck_agent_meeting_media_assertion_expiry
        CHECK (expires_at > seen_at),
    CONSTRAINT ck_agent_meeting_media_assertion_scope
        CHECK (
            (assertion_scope = 'SERVICE'
                AND tenant_id IS NULL
                AND meeting_id IS NULL
                AND resource_id IS NULL)
            OR
            (assertion_scope = 'RESOURCE'
                AND tenant_id IS NOT NULL
                AND meeting_id IS NOT NULL
                AND resource_id IS NOT NULL)
        )
);

CREATE INDEX ix_agent_meeting_media_assertion_expiry
    ON agent_meeting_media_assertion_replay (expires_at);

COMMENT ON TABLE agent_meeting_media_assertion_replay IS
    'Content-free one-time evidence for signed recording and transcript broker calls.';
