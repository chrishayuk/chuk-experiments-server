-- v0.9 — conclusion (written after runs complete, distinct from the
-- pre-existing hypothesis written before/at creation) and next_action
-- (what should happen next, or why this stopped) on experiment. Plain
-- nullable TEXT, matching hypothesis exactly — no new sub-object/table,
-- no verdict enum.
ALTER TABLE experiment
    ADD COLUMN IF NOT EXISTS conclusion TEXT,
    ADD COLUMN IF NOT EXISTS next_action TEXT;
