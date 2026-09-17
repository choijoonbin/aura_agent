CREATE INDEX idx_ai_artifact_drafts_home_title_pending
    ON ai_artifact_drafts (artifact_id)
    WHERE home_title_envelope IS NULL;

COMMENT ON INDEX idx_ai_artifact_drafts_home_title_pending IS
    'Bounds ordered SKIP LOCKED scans for the offline Home title projection backfill.';
