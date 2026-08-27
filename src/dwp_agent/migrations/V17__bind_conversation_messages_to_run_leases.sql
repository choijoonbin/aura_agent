ALTER TABLE ai_conversation_messages
    ADD COLUMN lease_generation BIGINT;

ALTER TABLE ai_conversation_messages
    ADD CONSTRAINT ck_ai_conversation_message_lease_generation
    CHECK (
        lease_generation IS NULL
        OR (lease_generation > 0 AND run_id IS NOT NULL)
    );

CREATE INDEX idx_ai_conversation_messages_run_lease
    ON ai_conversation_messages (run_id, lease_generation)
    WHERE lease_generation IS NOT NULL;

COMMENT ON COLUMN ai_conversation_messages.lease_generation IS
    'Run lease generation that produced the message; visible only after that generation completes.';
