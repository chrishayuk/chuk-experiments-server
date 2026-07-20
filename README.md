# chuk-experiments-server

The system of record for research experiments across programmes: every
experiment's write-up, every run's config/results, and pointers to whatever
artifacts (checkpoints, logs, datasets, harness code) it produced or used.
Three kinds of client read and write it — a human via a dashboard, an agent
via MCP, a training harness reporting run lifecycle — and all three go
through the same REST API over HTTP, never `service/` directly, so there
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
    -H "Authorization: Bearer $CHUK_EXPERIMENTS_API_KEY" \
    -F "file=@tokenizer_bench.py" -F "name=tok-v12-harness" -F "kind=other"
  ```
  (`$CHUK_EXPERIMENTS_API_KEY` — an environment variable, matching
  gpu-training-harness's own naming for this same server — never paste the
  literal key into the command itself; it's a credential leak for exactly
  the same reason large base64 content is a context leak.) The MCP tools
  (`upload_artifact_to_drive`/`upload_artifacts_batch`) exist for the case
  an agent already has small bytes in-context — their `content_base64`
  argument is necessarily emitted as literal text by the calling model, so
  it shows up in full in that model's own transcript; the server enforces a
  32KB decoded hard cap on both (rejecting anything larger with a 400) so
  that's a deliberate, deterministic limit rather than a judgment call —
  `upload-raw` is what anything bigger should use. Uploads are
  **content-addressed by
  (name, sha256)**: register the same harness script or dataset under the
  same name across many runs (or several times in one batch) and it's
  stored exactly once — every later reference just gets a lightweight
  pointer back to the original, tagged `role=used` instead of
  `role=produced`. `GET /artifacts/{id}/lineage` answers "which run made
  this, and which other runs have since reused it" for free from that same
  `(name, sha256, role)` grouping — no separate graph table. Large binaries
  (checkpoints) go straight to R2 via presigned URLs instead — bytes never
  transit this server at all.
- **Reference a git repo or Hugging Face Hub model/dataset directly** —
  for a harness that's already a GitHub repo, or a checkpoint already
  published on the Hub, `register_git_artifact`/`register_hf_artifact`
  record `git+https://github.com/{owner}/{repo}@{commit}` /
  `hf://model|dataset/{repo_id}@{revision}` pointers with no bytes moved
  at all. `POST /artifacts/{id}/verify` (or the `verify_artifact` MCP
  tool) does a real, on-demand check that the reference still resolves —
  a commit against GitHub's API, a revision against HF's file tree API,
  summing real file sizes against the artifact's own recorded `bytes` if
  given. This matters: a name/revision match alone isn't proof of
  anything — an HF repo can exist while missing most of its actual
  content. Results are cached (`verify_status`/`verified_at`/
  `verify_detail`), not re-checked automatically, since GitHub's
  unauthenticated API is capped at 60 requests/hour.
- **Pin a moving target.** `PUT /pins/{name}` points a named alias (e.g.
  `"tok-v12-tokenizer:latest"`) at a specific artifact and repoints it on
  demand, W&B-`"latest"`/`"best"`-style — so "the current best checkpoint"
  can be a stable thing to ask for even as which artifact that means keeps
  changing. Every pin is browsable on the dashboard's **Pins** screen, and
  a pinned artifact shows a "pinned as ..." badge inline on its run's
  detail page.
- **External refs screen.** Every `git+`/`hf://` artifact across every
  experiment is browsable in one place (`#/external-refs`) — reference,
  verify status, and last-checked time, so "which checkpoints are actually
  still resolvable on GitHub/HF" is a single screen instead of digging
  through individual runs.
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
  mint a key. The same screen lets each user set their own personal
  GitHub/Hugging Face token (encrypted at rest, never echoed back) so
  `verify_artifact` calls made under their key use their own rate limit
  instead of one shared server-wide token.
- **One process, two protocols.** Built on
  [chuk-mcp-server](https://github.com/chuk-mcp): REST endpoints
  (`@mcp.endpoint`) and MCP tools (`@mcp.tool`) live on the same
  `ChukMCPServer` instance and share one service layer. MCP tools are a thin
  forwarding layer that calls this server's own REST API over real loopback
  HTTP with the calling agent's bearer token — not a shortcut into
  `service/` — so an MCP client and a `curl` request hit identical
  validation and auth.

`experiment.id`/`run.id` are sortable strings, not serial integers or UUIDs:
`{PREFIX}-{YYYYMMDD}-{HHMMSS}-{5-digit sequence}`, e.g.
`RUN-20260718-160217-00397`. gpu-training-harness's own control plane uses the
same sortable shape for its own execution ids, but deliberately a different
prefix — `EXEC-…`, not `RUN-…` — since an EXEC execution and the RUN logical
research run it realises are different things (linked explicitly via the
run's `experiment_ref` field, never conflated by a shared id shape). `slug`
is separate and human-chosen (`cn-7`), auto-generated in the same format when
omitted.

R2 and the dashboard degrade gracefully rather than erroring when not
configured on a given deployment: artifact presign/upload routes reply `501
not_implemented`, `/login` replies `503` "sign-in not configured" — a
server missing either still serves everything else normally.

## Not built yet

- pgvector hybrid search, W&B summary sync (spec Phase 5).
- gpu-training-harness's own queue wired up to `/v1/queue` (the contract
  exists; the harness side of the integration doesn't yet).
- Full Google Drive archival of historical local-disk experiment data —
  `chuk-mlx` and `chris-experiments` are both done (archived, verified, and
  their local copies reclaimed); `larql`/`cell80`/a long tail of other repos
  are a much larger later pass, explicitly paused for now. See ROADMAP.md.

## Layout

```
src/chuk_experiments_server/
  constants.py        enums + named constants (Scope, ExperimentStatus, ArtifactRole, ...)
  models.py           Pydantic schemas — the single validation layer
  config.py           env-based settings (DATABASE_URL, R2_*, GOOGLE_*, ...)
  db.py               asyncpg pool + migration runner
  auth.py             bearer API key auth, scope checks (REST/MCP clients)
  webauth.py          Google sign-in for the dashboard (browser sessions)
  service/            business logic — the only thing that talks to Postgres. Split by
                       domain (programmes/experiments/runs/results/artifacts/users), with
                       __init__.py re-exporting the full public surface so every caller
                       still reaches it via `service.<name>`, unchanged
  errors.py           exception -> (status, json body) mapping
  serialization.py    Pydantic model -> plain JSON
  server.py           the shared ChukMCPServer instance
  rest/               REST endpoints (spec §4), registered onto `mcp` — same domain split
                       as service/
  tools/              MCP tools (spec §5) — thin forwarding layer over this server's own
                       REST API (internal_client.py), using the calling agent's own bearer
                       token; same domain split as service/ (no `users` submodule — no MCP
                       tool wraps dashboard user/key/token self-service)
  internal_client.py  loopback httpx client tools/ forwards through (MCP-to-REST only —
                       the dashboard SPA calls /v1/* directly from the browser, no proxy)
  web.py              OAuth flow (/login, /auth/callback), the SPA-shell route (/), and
                       the /static/{filename} route serving static/*.js
  markdown_render.py  write-up body_md -> sanitized HTML, computed server-side so the
                       SPA never needs its own markdown parser
  storage.py          R2 presigned upload/download
  drive_storage.py    Google Drive upload/folder helpers — backs the artifact upload
                       route and the archive_*_to_drive.py scripts
  external_refs.py    git+/hf:// artifact reference URI build/parse + real verification
                       against GitHub's/Hugging Face's REST APIs
  templates/          app.html (the SPA shell — CSS + a small inline <script> of
                       server-injected constants, no build step) + login.html
  static/             app.html's JS, split one file per dashboard screen plus shared
                       utilities/router, loaded via plain <script src> (see web.py)
  cli.py              `chuk-experiments-server migrate|serve|keys create|sweep`
migrations/
  001_init.sql              schema: programme/experiment/writeup/run/result/artifact/api_key
  002_string_ids.sql        experiment.id/run.id -> sortable string ids
  003_users_and_keys.sql    team/app_user tables; api_key gets team_id + created_by_user_id
  004_artifact_lineage.sql  artifact gets name/role; artifact_pin table (dedup + lineage + pins)
  005_artifact_produced_unique.sql  unique index on (name, sha256) where role='produced' (dedup race fix)
  006_artifact_verify.sql   artifact gets verify_status/verified_at/verify_detail (git+/hf:// verification)
  007_user_tokens.sql       app_user gets encrypted per-user GitHub/HF tokens (verify_artifact rate limits)
  008_artifact_uri_dedup.sql  (name, uri) dedup/lineage index for git+/hf:// refs (no sha256 to key on)
  009_experiment_conclusion_next_action.sql  experiment gets conclusion/next_action columns (what was learned, what's next)
  010_result_superseded.sql  result gets superseded_by — corrections stay linked, not just prose
  011_experiment_artifacts.sql  artifact gets an optional experiment_id parent (pre-run provenance)
scripts/
  migrate_chris_experiments.py          ../chris-experiments/INDEX.md (155 experiments, 8 programmes)
  migrate_chuk_mlx.py                    ../chuk-mlx/experiments/ (31, no central index — per-dir EXPERIMENT.md)
  migrate_chuk_mcp_lazarus.py            ~/.chuk-lazarus/experiments/*.json (172 of 1512 — rest is test noise)
  migrate_larql_aim_validation.py        ../larql/bench/aim-validation/*.json (3 — rest has no shared contract)
  archive_chuk_mlx_to_drive.py           chuk-mlx/experiments/ -> Google Drive (done, verified, local copy reclaimed)
  archive_chris_experiments_to_drive.py  chris-experiments/ -> Google Drive (done, verified, local copy reclaimed)
  audit_artifacts_for_git_refs.py        finds artifacts that could be git+/hf:// references instead of byte
                                          uploads — hashes real local git repos + does a real HF file/size diff
                                          (not name-matching); read-only unless given --apply-ids
  _drive_common.py                       shared OAuth/upload/manifest helpers for the archive_* scripts
  _migrate_common.py                     shared HTTP/experiment-creation helpers for the migrate_* scripts
  verify_harness_contract.py             E2E smoke test of the spec §6/§6a queue contract against a live server
  smoke_test.py                          read-only post-deploy schema-drift check (see .github/workflows/ci.yml);
                                          exercises every column added since migration 006 against real prod data
.github/workflows/
  ci.yml       lint + test on every push/PR; continuous deploy to Fly on push to main (migrations
               apply automatically as part of that deploy — see fly.toml's release_command), then a
               read-only smoke test (scripts/smoke_test.py) against production as a second line of
               defense in case schema and deployed code ever disagree for some other reason
  backup.yml   daily pg_dump of production -> gzipped, uploaded to R2 under backups/, 30-day
               rotation; plus an hourly one under backups/hourly/, rolling 24h window, for
               finer recovery granularity within the last day without piling up long-term
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

`chuk-experiments-server migrate` runs automatically on every deploy —
CI-driven or a manual `fly deploy` from your own machine, doesn't matter —
via `fly.toml`'s `[deploy] release_command`, which Fly runs in its own
ephemeral machine before the new release rolls out, aborting the deploy if
it fails. Idempotent (`ADD COLUMN IF NOT EXISTS` etc.), so this is safe on
every deploy regardless of whether that particular push carries a schema
change at all — nothing further to run by hand.

(This used to be a manual post-deploy step someone had to remember, which
is exactly how the 2026-07-20 incident in ROADMAP.md happened — migration
011 shipped, the step got missed, and `get_experiment` 500'd for every
experiment until caught by hand. CI's `smoke-test` job, `scripts/
smoke_test.py`, still hits production read-only right after every deploy
as a second line of defense, in case the schema and the deployed code ever
disagree for some other reason.)

`DATABASE_URL` is already set as a Fly secret. To provision a fresh app from
scratch elsewhere:

```bash
fly apps create <name>
fly secrets set DATABASE_URL='postgresql://...' --app <name>   # Neon connection string
fly deploy --app <name>
# then, once: DATABASE_URL=<neon-url> chuk-experiments-server migrate
```

Production is backed up daily and hourly (`.github/workflows/backup.yml`)
— Neon's own point-in-time recovery window is only 6h on the free plan
this project is on, which isn't a real backup, so a scheduled job dumps
the DB and uploads it (gzipped) to the R2 bucket. The daily dump lands
under `backups/`, pruned at 30 days; the hourly one lands under
`backups/hourly/`, pruned on a rolling 24h window — cheap fine-grained
recovery within the last day (without a separate midnight-cleanup job) on
top of the daily's longer retention.

Note the Dockerfile installs `uv` by copying the binary from
`ghcr.io/astral-sh/uv` (Astral's documented Docker pattern) rather than the
`curl | sh` installer — the installer's actual drop location didn't match
the assumed `~/.local/bin` on Fly's build image, and `uv pip install` failed
with `uv: not found` (see `Dockerfile` builder stage).

## More detail

See `ROADMAP.md` for phase-by-phase status, architectural decisions made
along the way, and what's next.
