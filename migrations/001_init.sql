-- chuk-experiments schema v0.1
-- Hierarchy: programme -> experiment -> writeup / run -> result / artifact

CREATE TABLE IF NOT EXISTS programme (
    id          BIGSERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS experiment (
    id           BIGSERIAL PRIMARY KEY,
    programme_id BIGINT NOT NULL REFERENCES programme(id) ON DELETE CASCADE,
    slug         TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','planned','running','completed','abandoned','superseded')),
    hypothesis   TEXT,
    design       JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags         TEXT[] NOT NULL DEFAULT '{}',
    search       TSVECTOR,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (programme_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_experiment_programme ON experiment(programme_id);
CREATE INDEX IF NOT EXISTS idx_experiment_status ON experiment(status);
CREATE INDEX IF NOT EXISTS idx_experiment_tags ON experiment USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_experiment_design ON experiment USING GIN(design);
CREATE INDEX IF NOT EXISTS idx_experiment_search ON experiment USING GIN(search);

CREATE TABLE IF NOT EXISTS writeup (
    id            BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
    version       INT NOT NULL,
    body_md       TEXT NOT NULL,
    author        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (experiment_id, version)
);

CREATE INDEX IF NOT EXISTS idx_writeup_experiment ON writeup(experiment_id);

-- `depends_on` is BIGINT[] (not the UUID[] the spec prose mentions) because
-- run.id is BIGSERIAL everywhere else in this same table — dependencies
-- reference that same id space.
CREATE TABLE IF NOT EXISTS run (
    id                BIGSERIAL PRIMARY KEY,
    experiment_id     BIGINT NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
    slug              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','claimed','running','completed','failed','killed','lost','cancelled')),
    priority          INT NOT NULL DEFAULT 0,
    depends_on        BIGINT[] NOT NULL DEFAULT '{}',
    workspec          JSONB NOT NULL DEFAULT '{}'::jsonb,
    requirements      JSONB NOT NULL DEFAULT '{}'::jsonb,
    est_seconds       INT,
    claimed_by        TEXT,
    claimed_at        TIMESTAMPTZ,
    lease_expires_at  TIMESTAMPTZ,
    claim_attempts    INT NOT NULL DEFAULT 0,
    backend           TEXT,
    harness_session_id TEXT,
    wandb_url         TEXT,
    config            JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at        TIMESTAMPTZ,
    ended_at          TIMESTAMPTZ,
    budget_seconds    INT,
    cost_usd          NUMERIC,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (experiment_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_run_experiment ON run(experiment_id);
CREATE INDEX IF NOT EXISTS idx_run_status ON run(status);
CREATE INDEX IF NOT EXISTS idx_run_config ON run USING GIN(config);
-- Queue: ready-run scan (claim/peek) and lease-expiry sweep are the two hot
-- queries the queue needs to stay cheap as the run table grows.
CREATE INDEX IF NOT EXISTS idx_run_queue_ready ON run(priority DESC, created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_run_depends_on ON run USING GIN(depends_on);
CREATE INDEX IF NOT EXISTS idx_run_lease_expiry ON run(lease_expires_at) WHERE status IN ('claimed', 'running');

CREATE TABLE IF NOT EXISTS result (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    value         NUMERIC,
    value_json    JSONB,
    verdict       TEXT CHECK (verdict IN ('pass','fail','inconclusive','n/a') OR verdict IS NULL),
    notes         TEXT,
    submitted_by  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_result_run ON result(run_id);
CREATE INDEX IF NOT EXISTS idx_result_name ON result(name);

CREATE TABLE IF NOT EXISTS artifact (
    id         BIGSERIAL PRIMARY KEY,
    run_id     BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('checkpoint','log','dataset','figure','tensor','other')),
    uri        TEXT NOT NULL,
    bytes      BIGINT,
    sha256     TEXT,
    meta       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifact_run ON artifact(run_id);
CREATE INDEX IF NOT EXISTS idx_artifact_kind ON artifact(kind);

CREATE TABLE IF NOT EXISTS api_key (
    id         BIGSERIAL PRIMARY KEY,
    key_hash   TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    scopes     TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

-- Keep experiment.search current from title + hypothesis + latest writeup body.
-- Not a generated column (Postgres GENERATED ALWAYS AS ... STORED can't run a
-- subquery against writeup), so it's maintained by triggers instead.

CREATE OR REPLACE FUNCTION experiment_search_refresh(p_experiment_id BIGINT) RETURNS void AS $$
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
$$ LANGUAGE sql;

CREATE OR REPLACE FUNCTION experiment_search_trigger() RETURNS trigger AS $$
BEGIN
    PERFORM experiment_search_refresh(NEW.id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_experiment_search ON experiment;
CREATE TRIGGER trg_experiment_search
    AFTER INSERT OR UPDATE OF title, hypothesis ON experiment
    FOR EACH ROW EXECUTE FUNCTION experiment_search_trigger();

CREATE OR REPLACE FUNCTION writeup_search_trigger() RETURNS trigger AS $$
BEGIN
    PERFORM experiment_search_refresh(NEW.experiment_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_writeup_search ON writeup;
CREATE TRIGGER trg_writeup_search
    AFTER INSERT ON writeup
    FOR EACH ROW EXECUTE FUNCTION writeup_search_trigger();
