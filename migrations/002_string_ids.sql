-- chuk-experiments schema v0.2 — sortable string ids for experiment/run.
--
-- experiment.id / run.id move from BIGSERIAL to a sortable TEXT id
-- ({PREFIX}-{YYYYMMDD}-{HHMMSS}-{5-digit sequence}, e.g.
-- "RUN-20260718-160217-00397" — matches the format already used by the
-- gpu-training-harness train server), regenerated in place for any rows
-- that already exist. programme/writeup/result/artifact/api_key keep plain
-- serial ids — nothing external ever addresses those directly.
--
-- Remapping happens via add-new-column / UPDATE...FROM / drop-old-column /
-- rename, not `ALTER COLUMN ... TYPE ... USING (subquery)` — Postgres's
-- ALTER COLUMN TYPE forbids a subquery in the USING expression ("cannot use
-- subquery in transform expression"), so a plain UPDATE...FROM (which has
-- no such restriction) does the actual remap instead.
--
-- Idempotent, matching 001_init.sql's convention: everything mutating runs
-- inside one DO block guarded by a check on experiment.id's current column
-- type, using dynamic SQL (EXECUTE) since PL/pgSQL can't run DDL directly.
-- 001_init.sql's own FTS-function setup carries the same guard in reverse
-- (skipping once id is already TEXT), so neither file fights the other on
-- a rerun.

CREATE SEQUENCE IF NOT EXISTS experiment_ref_seq;
CREATE SEQUENCE IF NOT EXISTS run_ref_seq;

DO $migration$
DECLARE
    already_migrated boolean;
    max_experiment_seq bigint;
    max_run_seq bigint;
    depends_on_before bigint;
    depends_on_after bigint;
BEGIN
    SELECT (data_type = 'text') INTO already_migrated
    FROM information_schema.columns
    WHERE table_name = 'experiment' AND column_name = 'id';

    IF already_migrated THEN
        RAISE NOTICE 'migration 002_string_ids already applied, skipping';
        RETURN;
    END IF;

    -- 1. Id maps for existing rows, in creation order (so both the
    -- timestamp and sequence components of the new id track original
    -- creation order).
    EXECUTE $sql$
        CREATE TEMP TABLE experiment_id_map AS
        SELECT id AS old_id,
               'EXP-' || to_char(created_at AT TIME ZONE 'UTC', 'YYYYMMDD') || '-' ||
                         to_char(created_at AT TIME ZONE 'UTC', 'HH24MISS') || '-' ||
                         lpad((row_number() OVER (ORDER BY created_at, id))::text, 5, '0') AS new_id
        FROM experiment
    $sql$;

    EXECUTE $sql$
        CREATE TEMP TABLE run_id_map AS
        SELECT id AS old_id,
               'RUN-' || to_char(created_at AT TIME ZONE 'UTC', 'YYYYMMDD') || '-' ||
                         to_char(created_at AT TIME ZONE 'UTC', 'HH24MISS') || '-' ||
                         lpad((row_number() OVER (ORDER BY created_at, id))::text, 5, '0') AS new_id
        FROM run
    $sql$;

    -- Belt-and-braces: row_number()'s strict per-table ordering makes a
    -- collision here impossible, but fail loudly rather than silently
    -- corrupt data if that assumption is ever wrong.
    IF EXISTS (SELECT 1 FROM experiment_id_map GROUP BY new_id HAVING COUNT(*) > 1) THEN
        RAISE EXCEPTION 'experiment_id_map has duplicate new_id values';
    END IF;
    IF EXISTS (SELECT 1 FROM run_id_map GROUP BY new_id HAVING COUNT(*) > 1) THEN
        RAISE EXCEPTION 'run_id_map has duplicate new_id values';
    END IF;

    -- 2. Drop objects that depend on the bigint id types: the FTS
    -- triggers/functions (parameter type changes below), the 4 FKs that
    -- cross tables, and the 5 plain indexes on the columns about to
    -- change type.
    EXECUTE 'DROP TRIGGER IF EXISTS trg_writeup_search ON writeup';
    EXECUTE 'DROP TRIGGER IF EXISTS trg_experiment_search ON experiment';
    EXECUTE 'DROP FUNCTION IF EXISTS writeup_search_trigger()';
    EXECUTE 'DROP FUNCTION IF EXISTS experiment_search_trigger()';
    EXECUTE 'DROP FUNCTION IF EXISTS experiment_search_refresh(BIGINT)';

    EXECUTE 'ALTER TABLE writeup DROP CONSTRAINT IF EXISTS writeup_experiment_id_fkey';
    EXECUTE 'ALTER TABLE run DROP CONSTRAINT IF EXISTS run_experiment_id_fkey';
    EXECUTE 'ALTER TABLE result DROP CONSTRAINT IF EXISTS result_run_id_fkey';
    EXECUTE 'ALTER TABLE artifact DROP CONSTRAINT IF EXISTS artifact_run_id_fkey';

    EXECUTE 'DROP INDEX IF EXISTS idx_writeup_experiment';
    EXECUTE 'DROP INDEX IF EXISTS idx_run_experiment';
    EXECUTE 'DROP INDEX IF EXISTS idx_run_depends_on';
    EXECUTE 'DROP INDEX IF EXISTS idx_result_run';
    EXECUTE 'DROP INDEX IF EXISTS idx_artifact_run';

    SELECT COALESCE(sum(array_length(depends_on, 1)), 0) INTO depends_on_before FROM run;

    -- 3. run.depends_on: BIGINT[] -> TEXT[], populated while run.id is
    -- still the original bigint (so the join back to run_id_map stays in
    -- one consistent id space). No FK ever protected this column's
    -- contents (see 001_init.sql's comment), so a dangling reference
    -- (already functionally inert — _READY_CLAUSE treats a missing
    -- dependency as unmet, never as vacuously satisfied) is simply
    -- dropped by the FILTER rather than erroring; the notice below
    -- surfaces if that happens.
    EXECUTE 'ALTER TABLE run ADD COLUMN depends_on_new TEXT[]';
    EXECUTE $sql$
        UPDATE run r SET depends_on_new = sub.new_depends
        FROM (
            SELECT r2.id AS run_id,
                   COALESCE(array_agg(m.new_id) FILTER (WHERE m.new_id IS NOT NULL), '{}') AS new_depends
            FROM run r2
            LEFT JOIN LATERAL unnest(r2.depends_on) AS dep(old_id) ON true
            LEFT JOIN run_id_map m ON m.old_id = dep.old_id
            GROUP BY r2.id
        ) sub
        WHERE r.id = sub.run_id
    $sql$;

    SELECT COALESCE(sum(array_length(depends_on_new, 1)), 0) INTO depends_on_after FROM run;
    IF depends_on_after < depends_on_before THEN
        RAISE NOTICE 'depends_on remap dropped % dangling reference(s) (% -> %)',
            depends_on_before - depends_on_after, depends_on_before, depends_on_after;
    END IF;

    -- 4. experiment.id, run.id, and the 4 FK columns: same add/populate
    -- pattern, still keyed off the original bigint values.
    EXECUTE 'ALTER TABLE experiment ADD COLUMN id_new TEXT';
    EXECUTE $sql$
        UPDATE experiment e SET id_new = m.new_id FROM experiment_id_map m WHERE m.old_id = e.id
    $sql$;

    EXECUTE 'ALTER TABLE run ADD COLUMN id_new TEXT';
    EXECUTE $sql$
        UPDATE run r SET id_new = m.new_id FROM run_id_map m WHERE m.old_id = r.id
    $sql$;

    EXECUTE 'ALTER TABLE writeup ADD COLUMN experiment_id_new TEXT';
    EXECUTE $sql$
        UPDATE writeup w SET experiment_id_new = m.new_id
        FROM experiment_id_map m WHERE m.old_id = w.experiment_id
    $sql$;

    EXECUTE 'ALTER TABLE run ADD COLUMN experiment_id_new TEXT';
    EXECUTE $sql$
        UPDATE run r SET experiment_id_new = m.new_id FROM experiment_id_map m WHERE m.old_id = r.experiment_id
    $sql$;

    EXECUTE 'ALTER TABLE result ADD COLUMN run_id_new TEXT';
    EXECUTE $sql$
        UPDATE result res SET run_id_new = m.new_id FROM run_id_map m WHERE m.old_id = res.run_id
    $sql$;

    EXECUTE 'ALTER TABLE artifact ADD COLUMN run_id_new TEXT';
    EXECUTE $sql$
        UPDATE artifact a SET run_id_new = m.new_id FROM run_id_map m WHERE m.old_id = a.run_id
    $sql$;

    -- 5. Swap old columns for new: drop the PKs (their backing indexes go
    -- with them), enforce NOT NULL/DEFAULT to match the originals, drop
    -- the old bigint columns, rename the new ones into place, drop the
    -- now-orphaned serial sequences.
    EXECUTE 'ALTER TABLE experiment DROP CONSTRAINT experiment_pkey';
    EXECUTE 'ALTER TABLE run DROP CONSTRAINT run_pkey';

    EXECUTE 'ALTER TABLE experiment ALTER COLUMN id_new SET NOT NULL';
    EXECUTE 'ALTER TABLE run ALTER COLUMN id_new SET NOT NULL';
    EXECUTE 'ALTER TABLE run ALTER COLUMN experiment_id_new SET NOT NULL';
    EXECUTE 'ALTER TABLE run ALTER COLUMN depends_on_new SET NOT NULL';
    EXECUTE $sql$ALTER TABLE run ALTER COLUMN depends_on_new SET DEFAULT '{}'$sql$;
    EXECUTE 'ALTER TABLE writeup ALTER COLUMN experiment_id_new SET NOT NULL';
    EXECUTE 'ALTER TABLE result ALTER COLUMN run_id_new SET NOT NULL';
    EXECUTE 'ALTER TABLE artifact ALTER COLUMN run_id_new SET NOT NULL';

    EXECUTE 'ALTER TABLE experiment DROP COLUMN id';
    EXECUTE 'ALTER TABLE run DROP COLUMN id';
    EXECUTE 'ALTER TABLE run DROP COLUMN experiment_id';
    EXECUTE 'ALTER TABLE run DROP COLUMN depends_on';
    EXECUTE 'ALTER TABLE writeup DROP COLUMN experiment_id';
    EXECUTE 'ALTER TABLE result DROP COLUMN run_id';
    EXECUTE 'ALTER TABLE artifact DROP COLUMN run_id';

    EXECUTE 'ALTER TABLE experiment RENAME COLUMN id_new TO id';
    EXECUTE 'ALTER TABLE run RENAME COLUMN id_new TO id';
    EXECUTE 'ALTER TABLE run RENAME COLUMN experiment_id_new TO experiment_id';
    EXECUTE 'ALTER TABLE run RENAME COLUMN depends_on_new TO depends_on';
    EXECUTE 'ALTER TABLE writeup RENAME COLUMN experiment_id_new TO experiment_id';
    EXECUTE 'ALTER TABLE result RENAME COLUMN run_id_new TO run_id';
    EXECUTE 'ALTER TABLE artifact RENAME COLUMN run_id_new TO run_id';

    EXECUTE 'DROP SEQUENCE IF EXISTS experiment_id_seq';
    EXECUTE 'DROP SEQUENCE IF EXISTS run_id_seq';

    -- 6. Re-add the 2 PKs, the 2 composite UNIQUE constraints that were
    -- silently dropped along with their participating experiment_id
    -- column (Postgres drops a multi-column UNIQUE constraint when one of
    -- its columns is dropped, without needing CASCADE or erroring — unlike
    -- a single-column PRIMARY KEY, which does error), 4 cross-table FKs,
    -- and 5 plain indexes.
    EXECUTE 'ALTER TABLE experiment ADD PRIMARY KEY (id)';
    EXECUTE 'ALTER TABLE run ADD PRIMARY KEY (id)';

    EXECUTE $sql$
        ALTER TABLE writeup ADD CONSTRAINT writeup_experiment_id_version_key UNIQUE (experiment_id, version)
    $sql$;
    EXECUTE $sql$
        ALTER TABLE run ADD CONSTRAINT run_experiment_id_slug_key UNIQUE (experiment_id, slug)
    $sql$;

    EXECUTE $sql$
        ALTER TABLE writeup ADD CONSTRAINT writeup_experiment_id_fkey
        FOREIGN KEY (experiment_id) REFERENCES experiment(id) ON DELETE CASCADE
    $sql$;
    EXECUTE $sql$
        ALTER TABLE run ADD CONSTRAINT run_experiment_id_fkey
        FOREIGN KEY (experiment_id) REFERENCES experiment(id) ON DELETE CASCADE
    $sql$;
    EXECUTE $sql$
        ALTER TABLE result ADD CONSTRAINT result_run_id_fkey
        FOREIGN KEY (run_id) REFERENCES run(id) ON DELETE CASCADE
    $sql$;
    EXECUTE $sql$
        ALTER TABLE artifact ADD CONSTRAINT artifact_run_id_fkey
        FOREIGN KEY (run_id) REFERENCES run(id) ON DELETE CASCADE
    $sql$;

    EXECUTE 'CREATE INDEX idx_writeup_experiment ON writeup(experiment_id)';
    EXECUTE 'CREATE INDEX idx_run_experiment ON run(experiment_id)';
    EXECUTE 'CREATE INDEX idx_run_depends_on ON run USING GIN(depends_on)';
    EXECUTE 'CREATE INDEX idx_result_run ON result(run_id)';
    EXECUTE 'CREATE INDEX idx_artifact_run ON artifact(run_id)';

    -- 7. Recreate the FTS functions/triggers — same bodies as
    -- 001_init.sql, just the id parameter's type changed to TEXT.
    EXECUTE $sql$
        CREATE OR REPLACE FUNCTION experiment_search_refresh(p_experiment_id TEXT) RETURNS void AS $body$
            UPDATE experiment e
            SET search =
                setweight(to_tsvector('english', coalesce(e.title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(e.hypothesis, '')), 'B') ||
                setweight(to_tsvector('english', coalesce((
                    SELECT w.body_md FROM writeup w
                    WHERE w.experiment_id = e.id
                    ORDER BY w.version DESC LIMIT 1
                ), '')), 'C')
            WHERE e.id = p_experiment_id;
        $body$ LANGUAGE sql
    $sql$;

    EXECUTE $sql$
        CREATE OR REPLACE FUNCTION experiment_search_trigger() RETURNS trigger AS $body$
        BEGIN
            PERFORM experiment_search_refresh(NEW.id);
            RETURN NEW;
        END;
        $body$ LANGUAGE plpgsql
    $sql$;
    EXECUTE $sql$
        CREATE TRIGGER trg_experiment_search
            AFTER INSERT OR UPDATE OF title, hypothesis ON experiment
            FOR EACH ROW EXECUTE FUNCTION experiment_search_trigger()
    $sql$;

    EXECUTE $sql$
        CREATE OR REPLACE FUNCTION writeup_search_trigger() RETURNS trigger AS $body$
        BEGIN
            PERFORM experiment_search_refresh(NEW.experiment_id);
            RETURN NEW;
        END;
        $body$ LANGUAGE plpgsql
    $sql$;
    EXECUTE $sql$
        CREATE TRIGGER trg_writeup_search
            AFTER INSERT ON writeup
            FOR EACH ROW EXECUTE FUNCTION writeup_search_trigger()
    $sql$;

    -- 8. Continue the ref sequences from whatever the backfill used, so
    -- new rows created after this migration don't reuse suffix numbers.
    SELECT max(right(new_id, 5)::bigint) INTO max_experiment_seq FROM experiment_id_map;
    SELECT max(right(new_id, 5)::bigint) INTO max_run_seq FROM run_id_map;
    IF max_experiment_seq IS NOT NULL THEN
        PERFORM setval('experiment_ref_seq', max_experiment_seq);
    END IF;
    IF max_run_seq IS NOT NULL THEN
        PERFORM setval('run_ref_seq', max_run_seq);
    END IF;
END;
$migration$;
