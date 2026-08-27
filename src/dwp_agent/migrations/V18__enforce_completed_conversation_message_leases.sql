UPDATE ai_agent_runs
   SET lease_generation = 1
 WHERE run_state = 'COMPLETED'
   AND lease_generation = 0;

UPDATE ai_conversation_messages message
   SET run_id = canonical_run.run_id,
       lease_generation = canonical_run.lease_generation
  FROM ai_conversations conversation,
       ai_agent_runs canonical_run
 WHERE message.conversation_id = conversation.conversation_id
   AND canonical_run.tenant_id = conversation.tenant_id
   AND canonical_run.user_id = conversation.user_id
   AND canonical_run.request_id = message.request_id
   AND canonical_run.run_state = 'COMPLETED'
   AND message.lease_generation IS NULL;

DELETE FROM ai_conversation_messages message
 USING ai_conversations conversation
 WHERE message.conversation_id = conversation.conversation_id
   AND NOT EXISTS (
       SELECT 1
         FROM ai_agent_runs canonical_run
        WHERE canonical_run.run_id = message.run_id
          AND canonical_run.tenant_id = conversation.tenant_id
          AND canonical_run.user_id = conversation.user_id
          AND canonical_run.request_id = message.request_id
          AND canonical_run.run_state = 'COMPLETED'
          AND canonical_run.lease_generation = message.lease_generation
   );

ALTER TABLE ai_conversation_messages
    DROP CONSTRAINT ck_ai_conversation_message_lease_generation,
    DROP CONSTRAINT ai_conversation_messages_run_id_fkey,
    ALTER COLUMN run_id SET NOT NULL,
    ALTER COLUMN lease_generation SET NOT NULL,
    ADD CONSTRAINT ck_ai_conversation_message_lease_generation
        CHECK (lease_generation > 0),
    ADD CONSTRAINT ai_conversation_messages_run_id_fkey
        FOREIGN KEY (run_id) REFERENCES ai_agent_runs(run_id) ON DELETE RESTRICT;

WITH completed_messages AS (
    SELECT conversation.conversation_id,
           COUNT(canonical_run.run_id)::INTEGER AS message_count,
           MAX(message.created_at) FILTER (
               WHERE canonical_run.run_id IS NOT NULL
           ) AS last_message_at
      FROM ai_conversations conversation
      LEFT JOIN ai_conversation_messages message
        ON message.conversation_id = conversation.conversation_id
      LEFT JOIN ai_agent_runs canonical_run
        ON canonical_run.run_id = message.run_id
       AND canonical_run.tenant_id = conversation.tenant_id
       AND canonical_run.user_id = conversation.user_id
       AND canonical_run.request_id = message.request_id
       AND canonical_run.run_state = 'COMPLETED'
       AND canonical_run.lease_generation = message.lease_generation
     GROUP BY conversation.conversation_id
)
UPDATE ai_conversations conversation
   SET message_count = completed_messages.message_count,
       last_message_at = COALESCE(
           completed_messages.last_message_at,
           conversation.created_at
       ),
       updated_at = GREATEST(
           conversation.updated_at,
           COALESCE(completed_messages.last_message_at, conversation.created_at)
       )
  FROM completed_messages
 WHERE completed_messages.conversation_id = conversation.conversation_id;

COMMENT ON COLUMN ai_conversation_messages.lease_generation IS
    'Required run lease generation; visible only when the same generation is completed.';
COMMENT ON CONSTRAINT ai_conversation_messages_run_id_fkey
    ON ai_conversation_messages IS
    'Run evidence must be retained for as long as its governed conversation messages.';
