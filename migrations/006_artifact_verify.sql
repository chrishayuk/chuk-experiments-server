-- v0.6 — cached verification for git+/hf:// reference artifacts. Plain
-- columns, not a history table: verify is on-demand/user-triggered only
-- (never auto-run on read, to respect GitHub's 60/hr unauthenticated rate
-- limit) — "latest check" is the only fact needed. Written only by
-- service.verify_artifact, never accepted from caller-supplied
-- ArtifactCreate/meta (same non-spoofable-by-caller rule as drive_url).
-- verify_detail carries the human-readable reason (e.g. "only 2.6GB of
-- 36.5GB present on Hugging Face") — the whole point of verify is knowing
-- *why*, not just a status word.
ALTER TABLE artifact ADD COLUMN IF NOT EXISTS verify_status TEXT
    CHECK (verify_status IN ('verified', 'missing', 'unverifiable'));
ALTER TABLE artifact ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE artifact ADD COLUMN IF NOT EXISTS verify_detail TEXT;
