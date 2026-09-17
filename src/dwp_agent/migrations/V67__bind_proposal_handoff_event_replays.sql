ALTER TABLE ai_proposal_handoff_events
    ADD COLUMN request_fingerprint CHAR(64);

UPDATE ai_proposal_handoff_events
   SET request_fingerprint = repeat('0', 64)
 WHERE request_fingerprint IS NULL;

ALTER TABLE ai_proposal_handoff_events
    ALTER COLUMN request_fingerprint SET NOT NULL,
    ADD CONSTRAINT ck_ai_proposal_handoff_event_request_fingerprint
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$');

COMMENT ON COLUMN ai_proposal_handoff_events.request_fingerprint IS
    'Tenant-keyed fingerprint of the exact create or observation command; legacy rows use a fail-closed sentinel.';
