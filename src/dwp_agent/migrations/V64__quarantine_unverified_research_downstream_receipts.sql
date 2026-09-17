WITH quarantined AS (
    UPDATE ai_research_deliveries AS delivery
       SET delivery_state = 'FAILED',
           receipt_id = NULL,
           receipt_envelope = NULL,
           safe_error_code = 'RESEARCH_DOWNSTREAM_RECEIPT_UNVERIFIED',
           recovery_hint =
               'Submit a new delivery after the allowlisted downstream provider is configured.',
           completed_at = NULL,
           updated_at = CURRENT_TIMESTAMP
     WHERE delivery.delivery_state = 'COMPLETED'
       AND delivery.delivery_type IN ('HANDOFF', 'SHARE')
       AND EXISTS (
           SELECT 1
             FROM ai_research_downstream_targets AS legacy
            WHERE legacy.delivery_id = delivery.delivery_id
       )
    RETURNING delivery.*
), event_rows AS (
    SELECT quarantined.*,
           md5(quarantined.delivery_id::text || ':provider-receipt-quarantine') AS digest
      FROM quarantined
)
INSERT INTO ai_research_delivery_events (
    event_id,
    delivery_id,
    tenant_id,
    user_id,
    actor_user_id,
    correlation_id,
    command_id,
    event_type,
    previous_state,
    current_state,
    occurred_at
)
SELECT (
           substr(digest, 1, 8) || '-' || substr(digest, 9, 4) || '-' ||
           substr(digest, 13, 4) || '-' || substr(digest, 17, 4) || '-' ||
           substr(digest, 21, 12)
       )::UUID,
       delivery_id,
       tenant_id,
       user_id,
       'system:research-receipt-quarantine',
       'migration:V64',
       (
           substr(md5(digest || ':command'), 1, 8) || '-' ||
           substr(md5(digest || ':command'), 9, 4) || '-' ||
           substr(md5(digest || ':command'), 13, 4) || '-' ||
           substr(md5(digest || ':command'), 17, 4) || '-' ||
           substr(md5(digest || ':command'), 21, 12)
       )::UUID,
       'UNVERIFIED_RECEIPT_QUARANTINED',
       'COMPLETED',
       'FAILED',
       CURRENT_TIMESTAMP
  FROM event_rows;

COMMENT ON TABLE ai_research_downstream_targets IS
    'Immutable legacy targets quarantined by V64; verified provider receipts are sealed on ai_research_deliveries.';
