SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE ai_agent_runs
    ADD COLUMN state_changed_at TIMESTAMPTZ;

ALTER TABLE ai_agent_runs
    ALTER COLUMN state_changed_at SET DEFAULT CURRENT_TIMESTAMP;

CREATE FUNCTION maintain_ai_agent_run_state_changed_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.run_state IS DISTINCT FROM OLD.run_state
       OR NEW.lease_generation IS DISTINCT FROM OLD.lease_generation THEN
        NEW.state_changed_at := CURRENT_TIMESTAMP;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_ai_agent_runs_state_changed_at
BEFORE UPDATE OF run_state, lease_generation ON ai_agent_runs
FOR EACH ROW EXECUTE FUNCTION maintain_ai_agent_run_state_changed_at();

COMMENT ON COLUMN ai_agent_runs.state_changed_at IS
    'Persisted source-ledger transition time; lease expiry is projected separately as an effective UNKNOWN transition.';
COMMENT ON FUNCTION maintain_ai_agent_run_state_changed_at() IS
    'Keeps state transition time correct for rolling-upgrade writers that do not yet dual-write state_changed_at.';
