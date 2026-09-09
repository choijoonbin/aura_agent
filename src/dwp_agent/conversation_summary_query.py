from __future__ import annotations


SUMMARY_COLUMNS = """conversation.conversation_id, conversation.locale,
       conversation.message_count, conversation.created_at,
       conversation.updated_at, conversation.last_message_at,
       conversation.title_envelope, conversation.title_nonce,
       conversation.title_ciphertext, conversation.encryption_key_version,
       conversation.retention_until,
       COALESCE(policy.legal_hold, FALSE),
       last_assistant.message_id, last_assistant.role,
       last_assistant.payload_envelope, last_assistant.payload_nonce,
       last_assistant.payload_ciphertext,
       last_assistant.encryption_key_version,
       last_assistant.provider"""


LATEST_VISIBLE_ASSISTANT_JOIN = """LEFT JOIN LATERAL (
    SELECT message.message_id, message.role,
           message.payload_envelope, message.payload_nonce,
           message.payload_ciphertext,
           message.encryption_key_version,
           completed_run.provider
      FROM ai_conversation_messages message
      JOIN ai_agent_runs completed_run
        ON completed_run.run_id = message.run_id
       AND completed_run.tenant_id = conversation.tenant_id
       AND completed_run.user_id = conversation.user_id
       AND completed_run.request_id = message.request_id
       AND completed_run.run_state = 'COMPLETED'
       AND completed_run.lease_generation = message.lease_generation
     WHERE message.conversation_id = conversation.conversation_id
       AND message.role = 'ASSISTANT'
     ORDER BY message.created_at DESC, message.message_id DESC
     LIMIT 1
) last_assistant ON TRUE"""
