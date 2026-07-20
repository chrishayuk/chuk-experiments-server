-- v0.10 — a result can be superseded by a later, corrected result. Real
-- incident: a wrong result stayed stamped verdict='pass' forever after being
-- corrected, with the correction recorded only in prose (another result's
-- notes) — an agent fetching the old result id in isolation, or ranking by
-- verdict, would carry the wrong conclusion forward. First self-referential
-- FK in this schema (artifact_pin is a separate table pointing into
-- artifact, not a row pointing at another row of the same table).
ALTER TABLE result ADD COLUMN IF NOT EXISTS superseded_by BIGINT REFERENCES result(id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'result_superseded_by_not_self'
    ) THEN
        ALTER TABLE result
            ADD CONSTRAINT result_superseded_by_not_self
            CHECK (superseded_by IS NULL OR superseded_by <> id);
    END IF;
END $$;
