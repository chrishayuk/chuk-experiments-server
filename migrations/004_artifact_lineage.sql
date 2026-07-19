-- chuk-experiments schema v0.4 — content-addressed artifact dedup, lineage,
-- and pins. Additive only: no existing column is retyped or removed, so
-- every existing artifact row and read path (get_run, find_checkpoints,
-- get_artifact, the download route) keeps working unchanged.
--
-- `name` is nullable — existing rows and simple one-off pointer
-- registrations don't need one. `role` defaults to 'produced', matching
-- every existing row's implicit meaning ("this run has this artifact").
-- Lineage falls out of grouping (name, sha256) by role, so no separate
-- join table is needed for it. `artifact_pin` is a small named-alias
-- table (W&B-style "latest"/"best") pointing at a specific artifact row.

ALTER TABLE artifact ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE artifact ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'produced'
    CHECK (role IN ('produced', 'used'));

CREATE INDEX IF NOT EXISTS idx_artifact_name_sha ON artifact(name, sha256) WHERE name IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifact_pin (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    artifact_id BIGINT NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
