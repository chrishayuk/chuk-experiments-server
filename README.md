# chuk-experiments-server

Experiment registry & results server — the system of record for research
experiments across programmes (record write-ups, runs, results, and pointers
to artifacts in object storage). Humans read it through the REST API (and,
later, a website); agents read and write it through MCP; a training harness
reports run lifecycle into it.

This is **Phase 0 + 1** of the spec (plus the §6a queue/lease system pulled
forward from Phase 3, since it lives in the same `run` table): the Postgres
schema, the REST API, and the MCP read/write tool set, all built on
[chuk-mcp-server](https://github.com/chuk-mcp) so REST endpoints and MCP
tools live in one process (`@mcp.endpoint` / `@mcp.tool` on the same
`ChukMCPServer` instance) sharing one service layer (`service.py`). Object
storage (R2), presigned uploads, and the read-only website are Phase 2+ and
not built yet — see "What's not here" below.

**Live at** https://chuk-experiments-server.fly.dev (Fly.io, `lhr`, scale-to-zero)
backed by Neon Postgres (project `chuk-experiment-server`,
`falling-darkness-22048271`), seeded from four sources — see "Seed data"
below.

## Layout

```
src/chuk_experiments_server/
  constants.py      enums + named constants (Scope, ExperimentStatus, ...)
  models.py         Pydantic schemas — the single validation layer
  config.py         env-based settings (DATABASE_URL, ...)
  db.py             asyncpg pool + migration runner
  auth.py           bearer API key auth, scope checks
  service.py        business logic — the only thing that talks to Postgres
  errors.py         exception -> (status, json body) mapping
  serialization.py  Pydantic model -> plain JSON
  server.py         the shared ChukMCPServer instance
  rest.py           REST endpoints (spec §4), registered onto `mcp`
  tools.py          MCP tools (spec §5), registered onto `mcp`
  cli.py            `chuk-experiments-server migrate|serve|keys create`
migrations/001_init.sql   schema: programme/experiment/writeup/run/result/artifact/api_key
scripts/
  migrate_chris_experiments.py    ../chris-experiments/INDEX.md (155 experiments, 8 programmes)
  migrate_chuk_mlx.py              ../chuk-mlx/experiments/ (31, no central index — per-dir EXPERIMENT.md)
  migrate_chuk_mcp_lazarus.py      ~/.chuk-lazarus/experiments/*.json (172 of 1512 — rest is test noise)
  migrate_larql_aim_validation.py  ../larql/bench/aim-validation/*.json (3 — rest has no shared contract)
```

## Local development

Needs a Postgres instance — `docker-compose.yml` runs one on `localhost:5433`.

```bash
cp .env.example .env          # adjust DATABASE_URL / EXPERIMENTS_BOOTSTRAP_KEY if needed
make db-up                    # start local Postgres
make dev-install
make migrate                  # apply schema + create the bootstrap API key
make serve                    # http://localhost:8000  (REST under /v1, MCP under /mcp)
```

Run any of the four migration scripts against it the same way, e.g.:

```bash
uv run python scripts/migrate_chris_experiments.py \
    --source ../chris-experiments/INDEX.md \
    --api-key <bootstrap-key-from-.env>
```

All four talk to a running server over the REST API (not the DB directly),
so point `--base-url` at whichever server you want seeded — local or the
live Fly deployment. Against Neon, expect each request to take noticeably
longer than against local Postgres (network round-trip to `us-east-1`); the
lazarus script alone issues ~1300 requests and took ~6 minutes end to end —
run it in the background (`nohup ... &`) rather than in the foreground with
a short timeout.

## Deployed (Fly.io + Neon)

Live at https://chuk-experiments-server.fly.dev. Neon project
`chuk-experiment-server` (`falling-darkness-22048271`, `aws-us-east-1`),
schema + bootstrap key applied via `chuk-experiments-server migrate`. To
redeploy after code changes:

```bash
fly deploy --app chuk-experiments-server
```

`DATABASE_URL` is already set as a Fly secret; `EXPERIMENTS_BOOTSTRAP_KEY`
was only used for the one-off `migrate` run against Neon and isn't needed as
a Fly secret since the key already exists in the DB. To provision a fresh
app from scratch elsewhere:

```bash
fly apps create <name>
fly secrets set DATABASE_URL='postgresql://...' --app <name>   # Neon connection string
fly deploy --app <name>
# then, once: DATABASE_URL=<neon-url> chuk-experiments-server migrate
```

Note the Dockerfile installs `uv` by copying the binary from
`ghcr.io/astral-sh/uv` (Astral's documented Docker pattern) rather than the
`curl | sh` installer — the installer's actual drop location didn't match
the assumed `~/.local/bin` on Fly's build image, and `uv pip install` failed
with `uv: not found` (see `Dockerfile` builder stage).

## What's not here yet (Phase 2+)

- R2 object storage, presigned upload/download (the `/artifacts/presign` and
  `/artifacts/{id}/download` routes exist and reply `501 not_implemented`).
- The read-only website (Phase 4).
- W&B summary sync (Phase 5).
