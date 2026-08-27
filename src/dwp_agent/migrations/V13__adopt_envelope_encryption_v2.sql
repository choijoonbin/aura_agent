ALTER TABLE ai_agent_runs
    ADD COLUMN response_envelope TEXT,
    ALTER COLUMN response_key_version DROP NOT NULL,
    ALTER COLUMN response_key_version DROP DEFAULT;

UPDATE ai_agent_runs
   SET response_key_version = NULL
 WHERE response_nonce IS NULL AND response_ciphertext IS NULL;

ALTER TABLE ai_agent_runs
    ADD CONSTRAINT ck_ai_agent_runs_response_encryption_v2 CHECK (
        (response_envelope IS NULL
            AND response_nonce IS NULL AND response_ciphertext IS NULL
            AND response_key_version IS NULL)
        OR (response_envelope IS NOT NULL AND response_envelope LIKE 'dwp2.%'
            AND response_nonce IS NULL AND response_ciphertext IS NULL
            AND response_key_version IS NULL)
        OR (response_envelope IS NULL
            AND response_nonce IS NOT NULL AND response_ciphertext IS NOT NULL
            AND response_key_version IS NOT NULL));

ALTER TABLE ai_conversations
    ADD COLUMN title_envelope TEXT,
    ALTER COLUMN title_nonce DROP NOT NULL,
    ALTER COLUMN title_ciphertext DROP NOT NULL,
    ALTER COLUMN encryption_key_version DROP NOT NULL,
    ALTER COLUMN encryption_key_version DROP DEFAULT;

ALTER TABLE ai_conversations
    ADD CONSTRAINT ck_ai_conversations_title_encryption_v2 CHECK (
        (title_envelope IS NOT NULL AND title_envelope LIKE 'dwp2.%'
            AND title_nonce IS NULL AND title_ciphertext IS NULL
            AND encryption_key_version IS NULL)
        OR (title_envelope IS NULL
            AND title_nonce IS NOT NULL AND title_ciphertext IS NOT NULL
            AND encryption_key_version IS NOT NULL));

ALTER TABLE ai_conversation_messages
    ADD COLUMN payload_envelope TEXT,
    ALTER COLUMN payload_nonce DROP NOT NULL,
    ALTER COLUMN payload_ciphertext DROP NOT NULL,
    ALTER COLUMN encryption_key_version DROP NOT NULL,
    ALTER COLUMN encryption_key_version DROP DEFAULT;

ALTER TABLE ai_conversation_messages
    ADD CONSTRAINT ck_ai_conversation_messages_payload_encryption_v2 CHECK (
        (payload_envelope IS NOT NULL AND payload_envelope LIKE 'dwp2.%'
            AND payload_nonce IS NULL AND payload_ciphertext IS NULL
            AND encryption_key_version IS NULL)
        OR (payload_envelope IS NULL
            AND payload_nonce IS NOT NULL AND payload_ciphertext IS NOT NULL
            AND encryption_key_version IS NOT NULL));

ALTER TABLE ai_answer_feedback
    ADD COLUMN comment_envelope TEXT,
    ALTER COLUMN encryption_key_version DROP NOT NULL,
    ALTER COLUMN encryption_key_version DROP DEFAULT,
    DROP CONSTRAINT ck_ai_answer_feedback_comment_pair;

UPDATE ai_answer_feedback
   SET encryption_key_version = NULL
 WHERE comment_nonce IS NULL AND comment_ciphertext IS NULL;

ALTER TABLE ai_answer_feedback
    ADD CONSTRAINT ck_ai_answer_feedback_comment_encryption_v2 CHECK (
        (comment_envelope IS NULL
            AND comment_nonce IS NULL AND comment_ciphertext IS NULL
            AND encryption_key_version IS NULL)
        OR (comment_envelope IS NOT NULL AND comment_envelope LIKE 'dwp2.%'
            AND comment_nonce IS NULL AND comment_ciphertext IS NULL
            AND encryption_key_version IS NULL)
        OR (comment_envelope IS NULL
            AND comment_nonce IS NOT NULL AND comment_ciphertext IS NOT NULL
            AND encryption_key_version IS NOT NULL));

ALTER TABLE ai_evaluation_cases
    ADD COLUMN prompt_envelope TEXT,
    ADD COLUMN expected_terms_envelope TEXT,
    ALTER COLUMN prompt_nonce DROP NOT NULL,
    ALTER COLUMN prompt_ciphertext DROP NOT NULL,
    ALTER COLUMN expected_terms_nonce DROP NOT NULL,
    ALTER COLUMN expected_terms_ciphertext DROP NOT NULL,
    ALTER COLUMN encryption_key_version DROP NOT NULL;

ALTER TABLE ai_evaluation_cases
    ADD CONSTRAINT ck_ai_evaluation_cases_payload_encryption_v2 CHECK (
        (prompt_envelope IS NOT NULL AND prompt_envelope LIKE 'dwp2.%'
            AND expected_terms_envelope IS NOT NULL
            AND expected_terms_envelope LIKE 'dwp2.%'
            AND prompt_nonce IS NULL AND prompt_ciphertext IS NULL
            AND expected_terms_nonce IS NULL AND expected_terms_ciphertext IS NULL
            AND encryption_key_version IS NULL)
        OR (prompt_envelope IS NULL AND expected_terms_envelope IS NULL
            AND prompt_nonce IS NOT NULL AND prompt_ciphertext IS NOT NULL
            AND expected_terms_nonce IS NOT NULL AND expected_terms_ciphertext IS NOT NULL
            AND encryption_key_version IS NOT NULL));

COMMENT ON COLUMN ai_agent_runs.response_envelope IS
    'Canonical DWP envelope v2 for the idempotent Agent response. Legacy columns are read-only.';
COMMENT ON COLUMN ai_conversations.title_envelope IS
    'Canonical DWP envelope v2 for the conversation title. Legacy columns are read-only.';
COMMENT ON COLUMN ai_conversation_messages.payload_envelope IS
    'Canonical DWP envelope v2 for the message payload. Legacy columns are read-only.';
COMMENT ON COLUMN ai_answer_feedback.comment_envelope IS
    'Canonical DWP envelope v2 for optional feedback text. Legacy columns are read-only.';
COMMENT ON COLUMN ai_evaluation_cases.prompt_envelope IS
    'Canonical DWP envelope v2 for the evaluation prompt. Legacy columns are read-only.';
COMMENT ON COLUMN ai_evaluation_cases.expected_terms_envelope IS
    'Canonical DWP envelope v2 for evaluation expectations. Legacy columns are read-only.';
