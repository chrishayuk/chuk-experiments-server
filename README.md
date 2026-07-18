# chuk-experiments-server

Experiment registry & results server — the system of record for research
experiments across programmes (record write-ups, runs, results, and pointers
to artifacts in object storage). Humans read it through a website; agents
read and write it through MCP; a training harness reports run lifecycle into
it — and all three go through the same REST API over HTTP, never
`service.py` directly, so every surface gets the same auth/validation path.

This covers **Phase 0 + 1** of the spec (the Postgres schema, the REST API,
and the MCP read/write tool set), the §6a queue/lease system pulled forward
from Phase 3 (lives in the same `run` table), **Phase 2** (R2 object
storage, presigned upload/download), and **Phase 4** (a read-only dashboard:
overview, search, browse, detail views, gated by Google sign-in restricted
to one email). All built on
[chuk-mcp-server](https://github.com/chuk-mcp) so REST endpoints and MCP
tools live in one process (`@mcp.endpoint` / `@mcp.tool` on the same
`ChukMCPServer` instance) sharing one service layer (`service.py`).
Phase 5 (pgvector hybrid search, W&B sync) isn't built yet — see "What's not
here" below and ROADMAP.md for what's next.

`experiment.id`/`run.id` are sortable strings, not serial integers or UUIDs:
`{PREFIX}-{YYYYMMDD}-{HHMMSS}-{5-digit sequence}`, e.g.
`RUN-20260718-160217-00397` — matching the format already used by the
gpu-training-harness train server. `slug` is still separate and
human-chosen (`cn-7`), auto-generated in the same format when omitted.

**Live at** https://chuk-experiments-server.fly.dev (Fly.io, `lhr`, scale-to-zero)
backed by Neon Postgres (project `chuk-experiment-server`,
`falling-darkness-22048271`), seeded from four sources — see "Seed data"
below. The dashboard needs `GOOGLE_CLIENT_ID`/`SECRET`/`REDIRECT_URI`,
`DASHBOARD_ALLOWED_EMAIL`, `SESSION_SECRET`, `INTERNAL_API_KEY` set as Fly
secrets before sign-in works in production — see `.env.example`.

## Layout

```
src/chuk_experiments_server/
  constants.py        enums + named constants (Scope, ExperimentStatus, ...)
  models.py           Pydantic schemas — the single validation layer
  config.py           env-based settings (DATABASE_URL, ...)
  db.py               asyncpg pool + migration runner
  auth.py             bearer API key auth, scope checks (REST/MCP clients)
  webauth.py          Google sign-in for the dashboard (browser sessions)
  service.py          business logic — the only thing that talks to Postgres
  errors.py           exception -> (status, json body) mapping
  serialization.py    Pydantic model -> plain JSON
  server.py           the shared ChukMCPServer instance
  rest.py             REST endpoints (spec §4), registered onto `mcp`
  tools.py            MCP tools (spec §5) — thin forwarding layer over this
                      server's own REST API (internal_client.py), using the
                      calling agent's own bearer token
  internal_client.py  loopback httpx client tools.py/web.py forward through
  web.py              dashboard routes (Phase 4), also REST-API-only
  markdown_render.py  write-up body_md -> sanitized HTML for the dashboard
  templates/          Jinja2 templates for the dashboard
  storage.py          R2 presigned upload/download (Phase 2)
  cli.py              `chuk-experiments-server migrate|serve|keys create|sweep`
migrations/
  001_init.sql          schema: programme/experiment/writeup/run/result/artifact/api_key
  002_string_ids.sql     experiment.id/run.id -> sortable string ids
scripts/
  migrate_chris_experiments.py    ../chris-experiments/INDEX.md (155 experiments, 8 programmes)
  migrate_chuk_mlx.py              ../chuk-mlx/experiments/ (31, no central index — per-dir EXPERIMENT.md)
  migrate_chuk_mcp_lazarus.py      ~/.chuk-lazarus/experiments/*.json (172 of 1512 — rest is test noise)
  migrate_larql_aim_validation.py  ../larql/bench/aim-validation/*.json (3 — rest has no shared contract)
  verify_harness_contract.py       E2E smoke test of the spec §6/§6a queue contract against a live server
```

## Local development

Needs a Postgres instance — `docker-compose.yml` runs one on `localhost:5433`.

```bash
cp .env.example .env          # adjust DATABASE_URL / EXPERIMENTS_BOOTSTRAP_KEY if needed
make db-up                    # start local Postgres
make dev-install
make migrate                  # apply schema + create the bootstrap API key
make serve                    # http://localhost:8000  (REST /v1, MCP /mcp, dashboard /)
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

## What's not here yet

- W&B summary sync, pgvector hybrid search (Phase 5).
- Google Drive archival of historical local-disk data (logs, checkpoints,
  runs never migrated as DB metadata) — scoped but not started; see
  ROADMAP.md.
- gpu-training-harness queue integration.

R2 (`/artifacts/presign`, `/artifacts/{id}/download`) and the dashboard
degrade gracefully rather than erroring when not configured on a given
deployment — R2 replies `501 not_implemented`, the dashboard's `/login`
replies `503` "sign-in not configured" — so a server missing either can
still serve everything else normally.
