-- chuk-experiments schema v0.5 — enforce the (name, sha256) dedup
-- invariant at the database level.
--
-- Without this, find_artifact_by_name_sha (a check) and register_artifact
-- (an insert) run as two separate statements with nothing to stop two
-- concurrent uploads of the same content from both missing the dedup hit
-- and both inserting role='produced' — at which point
-- get_artifact_lineage's `next(r for r in rows if r["role"] == "produced")`
-- silently picks one and drops the other from lineage entirely.
--
-- A partial unique index on (name, sha256) WHERE role = 'produced' turns
-- the loser of that race into a real, catchable UniqueViolationError
-- instead of a second silent 'produced' row. `name IS NOT NULL` keeps
-- plain pointer registrations (no name, not dedup-eligible) unaffected;
-- Postgres's standard NULL-is-never-equal-to-NULL semantics mean a
-- produced row missing sha256 never spuriously conflicts either.

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_produced_name_sha_unique
    ON artifact(name, sha256)
    WHERE role = 'produced' AND name IS NOT NULL;
