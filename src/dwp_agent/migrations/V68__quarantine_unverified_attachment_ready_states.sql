-- READY rows created before typed stage receipts existed cannot prove that AV,
-- DLP, parsing, OCR (when required), and indexing were provider-observed.
-- Fail closed; users may re-upload through the receipt-bound pipeline.
UPDATE ai_secure_attachments
   SET attachment_state = 'PARTIAL',
       revision = revision + 1,
       updated_at = CURRENT_TIMESTAMP
 WHERE attachment_state = 'READY';

COMMENT ON COLUMN ai_secure_attachments.stage_results_envelope IS
    'Encrypted attachment stages; terminal provider stages require target-bound typed receipt IDs and result digests.';
