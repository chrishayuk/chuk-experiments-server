-- v0.8 — content-addressed dedup/lineage for git+/hf:// reference artifacts,
-- which never have a sha256 (no bytes were ever hashed — the git commit or
-- HF revision recorded in the uri itself IS the content address). Mirrors
-- 005's (name, sha256) index exactly, scoped to sha256 IS NULL so the two
-- never overlap: registering the same (name, uri) twice now hits this
-- index on the second insert and falls back to role='used' via the same
-- catch-and-retry logic register_artifact already has for the sha256 case.
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_produced_name_uri_unique
    ON artifact(name, uri)
    WHERE role = 'produced' AND name IS NOT NULL AND sha256 IS NULL;
