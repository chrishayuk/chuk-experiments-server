-- chuk-experiments schema v0.3 — teams, dashboard users, and API-key ownership.
--
-- Adds a `team` concept (single seeded row for now — Chris's call: "for now
-- us just be a single team... saves us refactoring later") and an `app_user`
-- table backing the dashboard's Google sign-in with real roles (read/write/
-- admin) instead of a single hardcoded allowed-email check. `api_key` gets
-- `team_id` (backfilled, then made NOT NULL) and `created_by_user_id`
-- (stays nullable forever — CLI/bootstrap-created keys, e.g. the existing
-- `dev-local-key`, have no human user behind them).
--
-- Plain BIGSERIAL ids throughout: the sortable-string-id migration (002)
-- explicitly scoped itself to experiment/run only — nothing external
-- addresses team/app_user/api_key ids directly.
--
-- Idempotent like 001/002: IF NOT EXISTS / ON CONFLICT DO NOTHING throughout,
-- no DO-block guard needed since nothing here changes an existing column's
-- type (unlike 002's id-type change, which did need one).

CREATE TABLE IF NOT EXISTS team (
    id         BIGSERIAL PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO team (slug, name) VALUES ('default', 'CHUK')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE IF NOT EXISTS app_user (
    id         BIGSERIAL PRIMARY KEY,
    team_id    BIGINT NOT NULL REFERENCES team(id),
    email      TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL CHECK (role IN ('read', 'write', 'admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_app_user_team ON app_user(team_id);

ALTER TABLE api_key ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES team(id);
ALTER TABLE api_key ADD COLUMN IF NOT EXISTS created_by_user_id BIGINT REFERENCES app_user(id);

UPDATE api_key SET team_id = (SELECT id FROM team WHERE slug = 'default')
WHERE team_id IS NULL;

ALTER TABLE api_key ALTER COLUMN team_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_api_key_team ON api_key(team_id);
CREATE INDEX IF NOT EXISTS idx_api_key_created_by ON api_key(created_by_user_id);
