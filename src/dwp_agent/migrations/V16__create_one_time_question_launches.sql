CREATE TABLE ai_question_launch_tickets (
    launch_id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    session_family_id VARCHAR(160) NOT NULL,
    question_envelope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_ai_question_launch_expiry CHECK (
        expires_at > created_at
        AND expires_at <= created_at + INTERVAL '65 seconds'
    ),
    CONSTRAINT ck_ai_question_launch_envelope CHECK (
        question_envelope LIKE 'dwp2.%'
    )
);

CREATE INDEX idx_ai_question_launch_expiry
    ON ai_question_launch_tickets (expires_at);
CREATE INDEX idx_ai_question_launch_subject
    ON ai_question_launch_tickets (
        tenant_id, user_id, session_family_id, expires_at DESC
    );

CREATE TABLE ai_question_launch_rate_events (
    rate_event_id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    user_id VARCHAR(160) NOT NULL,
    session_family_id VARCHAR(160) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ai_question_launch_rate_subject
    ON ai_question_launch_rate_events (
        tenant_id, user_id, session_family_id, created_at DESC
    );
CREATE INDEX idx_ai_question_launch_rate_expiry
    ON ai_question_launch_rate_events (created_at);

COMMENT ON TABLE ai_question_launch_tickets IS
    'Opaque, encrypted, session-bound question handoffs between independently deployed DWP products.';
COMMENT ON COLUMN ai_question_launch_tickets.question_envelope IS
    'DWP2 envelope ciphertext; question plaintext must never be persisted or logged.';
COMMENT ON TABLE ai_question_launch_rate_events IS
    'Payload-free capacity evidence retained briefly for per-session launch throttling.';
