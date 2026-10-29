SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE ai_agent_run_audit_links (
    run_id UUID NOT NULL REFERENCES ai_agent_runs(run_id) ON DELETE CASCADE,
    lease_generation BIGINT NOT NULL,
    audit_id VARCHAR(128) NOT NULL,
    event_id UUID NOT NULL,
    link_state VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    linked_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, lease_generation),
    CONSTRAINT uk_ai_agent_run_audit_event UNIQUE (event_id),
    CONSTRAINT ck_ai_agent_run_audit_generation CHECK (lease_generation > 0),
    CONSTRAINT ck_ai_agent_run_audit_id CHECK (
        length(btrim(audit_id)) BETWEEN 1 AND 128),
    CONSTRAINT ck_ai_agent_run_audit_link_state CHECK (
        link_state IN ('PENDING', 'LINKED')),
    CONSTRAINT ck_ai_agent_run_audit_linked_at CHECK (
        (link_state = 'PENDING' AND linked_at IS NULL)
        OR (link_state = 'LINKED' AND linked_at IS NOT NULL))
);

CREATE TABLE ai_agent_run_audit_link_cutover (
    singleton SMALLINT PRIMARY KEY,
    cutover_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_ai_agent_run_audit_link_cutover_singleton CHECK (singleton = 1)
);

INSERT INTO ai_agent_run_audit_link_cutover (singleton, cutover_at)
VALUES (1, CURRENT_TIMESTAMP);

CREATE TABLE ai_agent_run_audit_legacy_receipts (
    event_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ai_agent_runs(run_id) ON DELETE CASCADE,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason VARCHAR(48) NOT NULL,
    CONSTRAINT ck_ai_agent_run_audit_legacy_reason CHECK (
        reason = 'PRE_V73_UNRECOVERABLE_LINK')
);

CREATE FUNCTION capture_ai_agent_run_audit_link()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    captured SMALLINT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.current_audit_id IS NOT NULL
           AND OLD.audit_record_id IS NOT NULL
           AND OLD.audit_link_state IS NOT NULL THEN
            INSERT INTO ai_agent_run_audit_links (
                run_id, lease_generation, audit_id, event_id, link_state, linked_at)
            VALUES (
                OLD.run_id,
                OLD.lease_generation,
                OLD.current_audit_id,
                OLD.audit_record_id,
                OLD.audit_link_state,
                CASE WHEN OLD.audit_link_state = 'LINKED'
                    THEN CURRENT_TIMESTAMP ELSE NULL END)
            ON CONFLICT (run_id, lease_generation) DO UPDATE
                SET link_state = CASE
                        WHEN ai_agent_run_audit_links.link_state = 'LINKED'
                             OR EXCLUDED.link_state = 'LINKED' THEN 'LINKED'
                        ELSE 'PENDING'
                    END,
                    linked_at = CASE
                        WHEN ai_agent_run_audit_links.link_state = 'LINKED'
                            THEN ai_agent_run_audit_links.linked_at
                        WHEN EXCLUDED.link_state = 'LINKED'
                            THEN COALESCE(EXCLUDED.linked_at, CURRENT_TIMESTAMP)
                        ELSE NULL
                    END
              WHERE ai_agent_run_audit_links.audit_id = EXCLUDED.audit_id
                AND ai_agent_run_audit_links.event_id = EXCLUDED.event_id
            RETURNING 1 INTO captured;

            IF captured IS NULL THEN
                RAISE EXCEPTION 'Agent audit identity changed within one lease generation';
            END IF;
        END IF;
    END IF;

    IF NEW.current_audit_id IS NULL
       OR NEW.audit_record_id IS NULL
       OR NEW.audit_link_state IS NULL THEN
        RETURN NEW;
    END IF;

    captured := NULL;

    INSERT INTO ai_agent_run_audit_links (
        run_id, lease_generation, audit_id, event_id, link_state, linked_at)
    VALUES (
        NEW.run_id,
        NEW.lease_generation,
        NEW.current_audit_id,
        NEW.audit_record_id,
        NEW.audit_link_state,
        CASE WHEN NEW.audit_link_state = 'LINKED' THEN CURRENT_TIMESTAMP ELSE NULL END)
    ON CONFLICT (run_id, lease_generation) DO UPDATE
        SET link_state = CASE
                WHEN ai_agent_run_audit_links.link_state = 'LINKED'
                     OR EXCLUDED.link_state = 'LINKED' THEN 'LINKED'
                ELSE 'PENDING'
            END,
            linked_at = CASE
                WHEN ai_agent_run_audit_links.link_state = 'LINKED'
                    THEN ai_agent_run_audit_links.linked_at
                WHEN EXCLUDED.link_state = 'LINKED'
                    THEN COALESCE(EXCLUDED.linked_at, CURRENT_TIMESTAMP)
                ELSE NULL
            END
      WHERE ai_agent_run_audit_links.audit_id = EXCLUDED.audit_id
        AND ai_agent_run_audit_links.event_id = EXCLUDED.event_id
    RETURNING 1 INTO captured;

    IF captured IS NULL THEN
        RAISE EXCEPTION 'Agent audit identity changed within one lease generation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_ai_agent_runs_capture_audit_link
AFTER INSERT OR UPDATE OF lease_generation, current_audit_id, audit_record_id, audit_link_state
ON ai_agent_runs
FOR EACH ROW EXECUTE FUNCTION capture_ai_agent_run_audit_link();

COMMENT ON TABLE ai_agent_run_audit_links IS
    'Immutable per-attempt Agent audit identities and delivery-link state; retries never overwrite earlier pending receipts.';
COMMENT ON COLUMN ai_agent_run_audit_links.event_id IS
    'Deterministic central audit event identifier used for exact idempotent collector acknowledgement.';
COMMENT ON TABLE ai_agent_run_audit_link_cutover IS
    'Singleton cutover boundary that limits legacy mismatch receipts to runs created before V73.';
COMMENT ON TABLE ai_agent_run_audit_legacy_receipts IS
    'Quarantined receipt for a collector-accepted pre-V73 spool event whose overwritten attempt link cannot be reconstructed; never updates the current run projection.';
COMMENT ON FUNCTION capture_ai_agent_run_audit_link() IS
    'Captures both the previous and current exact per-generation audit identities from old and new writers during rolling upgrades.';
