ALTER TABLE ai_agent_proposals
    ADD COLUMN request_fingerprint CHAR(64);

-- V19 fingerprints were unkeyed. Replace them with non-content-derived sentinels so
-- legacy rows fail closed on replay without retaining a dictionary oracle.
UPDATE ai_agent_proposals
   SET request_fingerprint =
       md5(proposal_id::text || clock_timestamp()::text || random()::text)
       || md5(random()::text || tenant_id::text || proposal_id::text);

ALTER TABLE ai_agent_proposals
    ALTER COLUMN request_fingerprint SET NOT NULL,
    ADD CONSTRAINT ck_ai_agent_proposal_request_fingerprint
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    DROP CONSTRAINT ck_ai_agent_proposal_hash,
    DROP COLUMN content_hash;

ALTER TABLE ai_agent_proposal_events
    ADD COLUMN request_fingerprint CHAR(64),
    ADD CONSTRAINT ck_ai_agent_proposal_event_request_fingerprint
        CHECK (
            request_fingerprint IS NULL
            OR request_fingerprint ~ '^[0-9a-f]{64}$'
        );

COMMENT ON COLUMN ai_agent_proposals.request_fingerprint IS
    'Tenant- and purpose-scoped keyed HMAC of the immutable creation command payload.';
COMMENT ON COLUMN ai_agent_proposal_events.request_fingerprint IS
    'Tenant- and purpose-scoped keyed HMAC of a decision command payload; NULL only for pre-V20 evidence.';
