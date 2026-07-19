# chuk-experiments-server

The system of record for research experiments across programmes: every
experiment's write-up, every run's config/results, and pointers to whatever
artifacts (checkpoints, logs, datasets, harness code) it produced or used.
Three kinds of client read and write it — a human via a dashboard, an agent
via MCP, a training harness reporting run lifecycle — and all three go
through the same REST API over HTTP, never `service.py` directly, so there
is exactly one auth/validation path regardless of who's calling.

**Live at** https://chuk-experiments-server.fly.dev (Fly.io, scale-to-zero,
Neon Postgres). Source: https://github.com/chrishayuk/chuk-experiments-server.

## What it does

- **Track experiments and runs.** An experiment belongs to a programme, has
  a hypothesis and a versioned Markdown write-up; each run under it has a
  config, a status (`queued`/`running`/`completed`/`failed`/...), a cost,
  and any number of named results (metric → value) and artifacts.
- **Store and dedup artifacts.** Register a pointer to anything already
  reachable (`s3://`, `gdrive://`, `https://`) — never a local `file://`
  path — or hand this server actual bytes and it uploads them to Google
  Drive for you. Three ways to hand over bytes: `POST
  /runs/{id}/artifacts/upload` (JSON + base64, single file), `.../upload-batch`
  (JSON + base64, several files in one round trip), or `.../upload-raw`
  (multipart, one real file straight off disk — the one to reach for from
  a shell, since curl streams the file directly and nothing but the JSON
  response ever has to pass through an agent's own context):
  ```bash
  curl -X POST https://chuk-experiments-server.fly.dev/v1/runs/$RUN_ID/artifacts/upload-raw \
    -H "Authorization: Bearer $KEY" \
    -F "file=@tokenizer_bench.py" -F "name=tok-v12-harness" -F "kind=other"
  ```
  The MCP tools (`upload_artifact_to_drive`/`upload_artifacts_batch`) exist
  for the case an agent already has small bytes in-context — their
  `content_base64` argument is necessarily emitted as literal text by the
  calling model, so it shows up in full in that model's own transcript;
  fine for a short snippet, real friction for anything larger, which is
  exactly what `upload-raw` avoids. Uploads are **content-addressed by
  (name, sha256)**: register the same harness script or dataset under the
  same name across many runs (or several times in one batch) and it's
  stored exactly once — every later reference just gets a lightweight
  pointer back to the original, tagged `role=used` instead of
  `role=produced`. `GET /artifacts/{id}/lineage` answers "which run made
  this, and which other runs have since reused it" for free from that same
  `(name, sha256, role)` grouping — no separate graph table. Large binaries
  (checkpoints) go straight to R2 via presigned URLs instead — bytes never
  transit this server at all.
- **Pin a moving target.** `PUT /pins/{name}` points a named alias (e.g.
  `"tok-v12-tokenizer:latest"`) at a specific artifact and repoints it on
  demand, W&B-`"latest"`/`"best"`-style — so "the current best checkpoint"
  can be a stable thing to ask for even as which artifact that means keeps
  changing. Every pin is browsable on the dashboard's **Pins** screen, and
  a pinned artifact shows a "pinned as ..." badge inline on its run's
  detail page.
- **Queue and lease runs for a training harness.** A harness can peek the
  queue, atomically claim a batch (`FOR UPDATE SKIP LOCKED`, greedy
  bin-packing by priority/estimated seconds), respect dependency ordering
  between runs, renew its lease while working, and have expired
  claims swept back to `queued` (or `lost` after too many attempts) —
  without ever touching Postgres directly.
- **Dashboard.** A client-side SPA (`templates/app.html`: one static shell,
  vanilla-JS `fetch()` + hash routing against `/v1/*`, no build step) —
  overview, sortable/filterable experiment browsing, search, run detail
  (with artifact lineage inline), gated by Google sign-in. Any signed-in
  user can self-service generate/revoke their own MCP API keys from the
  **Team** screen, scoped to their role's ceiling (read/write/admin); admins
  can add or revoke collaborators. No more shelling into the server just to
  mint a key.
- **One process, two protocols.** Built on
  [chuk-mcp-server](https://github.com/chuk-mcp): REST endpoints
  (`@mcp.endpoint`) and MCP tools (`@mcp.tool`) live on the same
  `ChukMCPServer` instance and share one service layer. MCP tools are a thin
  forwarding layer that calls this server's own REST API over real loopback
  HTTP with the calling agent's bearer token — not a shortcut into
  `service.py` — so an MCP client and a `curl` request hit identical
  validation and auth.

`experiment.id`/`run.id` are sortable strings, not serial integers or UUIDs:
`{PREFIX}-{YYYYMMDD}-{HHMMSS}-{5-digit sequence}`, e.g.
`RUN-20260718-160217-00397` — matching the format gpu-training-harness's own
train server already uses, so both sort chronologically as plain strings.
`slug` is separate and human-chosen (`cn-7`), auto-generated in the same
format when omitted.

R2 and the dashboard degrade gracefully rather than erroring when not
configured on a given deployment: artifact presign/upload routes reply `501
not_implemented`, `/login` replies `503` "sign-in not configured" — a
server missing either still serves everything else normally.

## Not built yet

- pgvector hybrid search, W&B summary sync (spec Phase 5).
- gpu-training-harness's own queue wired up to `/v1/queue` (the contract
  exists; the harness side of the integration doesn't yet).
- Full Google Drive archival of historical local-disk experiment data —
  `chuk-mlx` is done; `chris-experiments` is in progress. See ROADMAP.md.

## Layout

```
src/chuk_experiments_server/
  constants.py        enums + named constants (Scope, ExperimentStatus, ArtifactRole, ...)
  models.py           Pydantic schemas — the single validation layer
  config.py           env-based settings (DATABASE_URL, R2_*, GOOGLE_*, ...)
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
  internal_client.py  loopback httpx client tools.py forwards through (MCP-to-REST only —
                       the dashboard SPA calls /v1/* directly from the browser, no proxy)
  web.py              OAuth flow (/login, /auth/callback) + the one SPA-shell route (/)
  markdown_render.py  write-up body_md -> sanitized HTML, computed server-side so the
                       SPA never needs its own markdown parser
  storage.py          R2 presigned upload/download
  drive_storage.py    Google Drive upload/folder helpers — backs the artifact upload
                       route and the archive_*_to_drive.py scripts
  templates/          app.html (the SPA shell — CSS + vanilla JS, no build step) + login.html
  cli.py              `chuk-experiments-server migrate|serve|keys create|sweep`
migrations/
  001_init.sql              schema: programme/experiment/writeup/run/result/artifact/api_key
  002_string_ids.sql        experiment.id/run.id -> sortable string ids
  003_users_and_keys.sql    team/app_user tables; api_key gets team_id + created_by_user_id
  004_artifact_lineage.sql  artifact gets name/role; artifact_pin table (dedup + lineage + pins)
scripts/
  migrate_chris_experiments.py          ../chris-experiments/INDEX.md (155 experiments, 8 programmes)
  migrate_chuk_mlx.py                    ../chuk-mlx/experiments/ (31, no central index — per-dir EXPERIMENT.md)
  migrate_chuk_mcp_lazarus.py            ~/.chuk-lazarus/experiments/*.json (172 of 1512 — rest is test noise)
  migrate_larql_aim_validation.py        ../larql/bench/aim-validation/*.json (3 — rest has no shared contract)
  archive_chuk_mlx_to_drive.py           chuk-mlx/experiments/ -> Google Drive (done, verified, local copy reclaimed)
  archive_chris_experiments_to_drive.py  chris-experiments/ -> Google Drive (in progress)
  _drive_common.py                       shared OAuth/upload/manifest helpers for the archive_* scripts
  verify_harness_contract.py             E2E smoke test of the spec §6/§6a queue contract against a live server
.github/workflows/
  ci.yml       lint + test on every push/PR; continuous deploy to Fly on push to main
  backup.yml   daily pg_dump of production -> gzipped, uploaded to R2, 30-day rotation
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

R2, Google sign-in, and Google Drive artifact storage are each optional —
see the comments in `.env.example` for what happens when a given block is
left unset, and what each one needs.

Run any of the four historical-data migration scripts against a running
server the same way, pointing `--base-url` at local or the live Fly
deployment:

```bash
uv run python scripts/migrate_chris_experiments.py \
    --source ../chris-experiments/INDEX.md \
    --api-key <bootstrap-key-from-.env>
```

Against Neon, expect each request to take noticeably longer than against
local Postgres (network round-trip to `us-east-1`) — the lazarus script
alone issues ~1300 requests and takes several minutes end to end; run it in
the background (`nohup ... &`) rather than in the foreground with a short
timeout.

## Deployed (Fly.io + Neon)

Live at https://chuk-experiments-server.fly.dev. Neon project
`chuk-experiment-server` (`falling-darkness-22048271`, `aws-us-east-1`),
schema + bootstrap key applied via `chuk-experiments-server migrate`. To
redeploy after code changes:

```bash
fly deploy --app chuk-experiments-server
```

**`fly deploy` (and CI's deploy job) only restarts the container — neither
runs `migrate` against production.** A new migration file does nothing in
production until it's applied explicitly:

```bash
fly ssh console --app chuk-experiments-server -C "chuk-experiments-server migrate"
```

Forgetting this after a schema change means the new tables/columns simply
don't exist yet on the live DB, which surfaces as a REST 500 the first time
something touches them — not a crash on deploy. Always check whether a
change needs this step in addition to `deploy`.

`DATABASE_URL` is already set as a Fly secret. To provision a fresh app from
scratch elsewhere:

```bash
fly apps create <name>
fly secrets set DATABASE_URL='postgresql://...' --app <name>   # Neon connection string
fly deploy --app <name>
# then, once: DATABASE_URL=<neon-url> chuk-experiments-server migrate
```

Production is backed up daily (`.github/workflows/backup.yml`) — Neon's own
point-in-time recovery window is only 6h on the free plan this project is
on, which isn't a real backup, so a scheduled job dumps the DB and uploads
it (gzipped) to the R2 bucket under `backups/`, pruning anything older than
30 days.

Note the Dockerfile installs `uv` by copying the binary from
`ghcr.io/astral-sh/uv` (Astral's documented Docker pattern) rather than the
`curl | sh` installer — the installer's actual drop location didn't match
the assumed `~/.local/bin` on Fly's build image, and `uv pip install` failed
with `uv: not found` (see `Dockerfile` builder stage).

## More detail

See `ROADMAP.md` for phase-by-phase status, architectural decisions made
along the way, and what's next.
