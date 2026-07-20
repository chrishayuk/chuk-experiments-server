-- v0.11 — first-class experiment-level artifacts. Real gap: a
-- pre-registration document is the paradigm experiment-level artifact (it
-- exists before any run does), but artifacts could only ever attach to a
-- run — its sha256/commit provenance ended up in write-up prose instead,
-- unqueryable via get_artifact_lineage/verify_artifact. Additive: existing
-- run-scoped rows are untouched (run_id stays set, experiment_id stays
-- null), so every existing read path keeps working unchanged.
ALTER TABLE artifact ADD COLUMN IF NOT EXISTS experiment_id TEXT REFERENCES experiment(id) ON DELETE CASCADE;
ALTER TABLE artifact ALTER COLUMN run_id DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'artifact_parent_check'
    ) THEN
        -- Exactly one parent set, never both, never neither. `<>` between
        -- two booleans is Postgres's XOR.
        ALTER TABLE artifact
            ADD CONSTRAINT artifact_parent_check
            CHECK ((run_id IS NOT NULL) <> (experiment_id IS NOT NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_artifact_experiment ON artifact(experiment_id) WHERE experiment_id IS NOT NULL;
