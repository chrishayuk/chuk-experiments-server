# Roadmap

Status of each phase from the original spec, plus the architectural
decisions made along the way that the spec didn't originally cover.

## Done

- **Phase 0/1** — Postgres schema, REST API (spec §4), MCP read/write tool
  set (spec §5), all sharing one service layer (`service.py`).
- **§6a queue/lease system** — pulled forward from Phase 3 since it lives in
  the same `run` table: atomic claim (`FOR UPDATE SKIP LOCKED` + greedy
  bin-packing by priority/`est_seconds`), dependency-gated readiness,
  lease renewal, expiry sweep (`claimed`/`running` → `queued`, or `lost`
  after `DEFAULT_MAX_CLAIM_ATTEMPTS`).
- **Phase 2** — R2 object storage. Presigned upload (`/artifacts/presign`)
  and download (`/artifacts/{id}/download`), a dedicated `chuk-experiments`
  R2 bucket (own API token, separate from gpu-training-harness's
  `chuk-train` bucket — checkpoints are training artifacts, not experiment
  records; `register_artifact` is bucket-agnostic, so a pointer to a
  `chuk-train` checkpoint still works fine as an artifact URI).
- **Phase 4** — dashboard: overview, browse/filter, experiment detail
  (rendered write-up), run detail, search, artifact download. **Rewritten as
  a client-side SPA** (`templates/app.html`): one static shell page, all
  navigation/data-loading is vanilla-JS `fetch()` + hash routing against
  `/v1/*` directly, no build step — matching gpu-training-harness's own
  dashboard, which was the explicit ask after the original server-rendered
  Jinja2 version felt comparatively flat. `auth.require_scope_from_request`
  accepts the dashboard's Google session cookie as an alternative to a
  bearer token for Scope.READ, so the browser needs no server-side proxy;
  the old 6-Jinja2-page/proxy-layer version of `web.py` is gone. Includes
  real offset-based pagination (fixed 25-row pages, proper Prev/Next) on the
  Experiments and Search views, added the same day once 361 real
  experiments made the original "growing limit" approach visibly wasteful.
  Google sign-in is **live in production** — `GOOGLE_CLIENT_ID`/`SECRET`/
  `REDIRECT_URI`, `DASHBOARD_ALLOWED_EMAIL`, `SESSION_SECRET` are set as Fly
  secrets, the callback URL is registered, and real sign-in is confirmed
  working end-to-end (verified via a live MCP-client connection minting a
  key through the dashboard itself).
- **Teams, dashboard roles, self-service API-key management** — `team`
  (single seeded row today) and `app_user` (email/role: read/write/admin)
  tables back the dashboard's Google sign-in instead of a single hardcoded
  allowed-email check. A new **Team** screen (`#/team`) lets any signed-in
  user generate/revoke their own MCP API keys, capped at their role's scope
  ceiling (`constants.ROLE_SCOPE_CEILING`), and lets admins add/revoke
  collaborators — no more `fly ssh console` just to mint a key. Gated by a
  separate `auth.require_dashboard_role` axis (bearer-ADMIN "system
  operator", or a real signed-in sufficiently-privileged user; deliberately
  no unconfigured-auth free pass, since minting credentials is a materially
  different risk than reading experiment data). Refuses to revoke the last
  remaining admin. Confirmed live end-to-end: a real MCP client connection
  (`claude mcp add`) using a key minted through the dashboard by a real
  Google sign-in.
- **Daily production backup** (`.github/workflows/backup.yml`) — Neon's
  built-in point-in-time recovery is only a 6h rolling window on the free
  plan this project is on, not a real backup. A scheduled job `pg_dump`s
  production, gzips it, uploads to the existing R2 bucket under `backups/`,
  and prunes anything older than 30 days.
- **REST-API-only architecture** — MCP tools (`tools.py`) call this server's
  own REST API over real HTTP (`internal_client.py`, a loopback `httpx`
  client), forwarding the calling agent's own bearer token, never
  `service.py` directly. The dashboard now does the same thing one layer
  further out: the browser itself calls `/v1/*` directly (its Google
  session cookie satisfies `Scope.READ`), no server-side proxy at all. One
  code path for auth/validation regardless of which surface is calling.
- **Sortable string ids** — `experiment.id`/`run.id` moved from
  `BIGSERIAL` to `{PREFIX}-{YYYYMMDD}-{HHMMSS}-{5-digit sequence}` (e.g.
  `RUN-20260718-160217-00397`), matching the format already used by the
  gpu-training-harness train server — sorts chronologically as a plain
  string. `programme`/`writeup`/`result`/`artifact`/`api_key` keep plain
  serial ids (nothing external addresses those directly). Existing rows
  were regenerated in place (`migrations/002_string_ids.sql`), not just new
  ones going forward. `slug` (human-facing, used in URLs) is unaffected
  where explicitly chosen (`cn-7`); auto-generated in the same format when
  omitted, for both experiments and runs.
- Seeded from 4 historical sources (chris-experiments, chuk-mlx,
  chuk-mcp-lazarus, larql aim-validation) — 361 experiments as of the last
  full migration, all backfilled with `status=completed` and original
  nuance preserved as tags.
- CI/CD (GitHub Actions): lint+test on every push/PR, continuous deploy to
  Fly on push to `main` gated on tests passing.
- 90%+ test coverage per file (98% overall, 256 tests), `ruff check`/`ruff
  format` clean across the repo.

## Next

1. **Google Drive archival of historical local-disk data** — in progress.
   A survey found ~300GB of unmigrated data across the known sources,
   overwhelmingly dominated by `larql/output/`'s 252GB of per-model
   `.vindex`/checkpoint bundles (arguably general model storage rather than
   per-experiment artifacts, and a materially different shape from the
   other sources). Sequencing:
   - `chuk-mlx/experiments/` (~1.6G) — **done**: archived, verified
     (896/898 files; the 2 unarchived are `.pyc` bytecode caches, a
     deliberate exclusion), local copy reclaimed.
   - `chris-experiments/` (~19G) — **in progress**, running against
     production.
   - `larql/output/` (252G) and `cell80/experiments/` (~20G, a 5th
     experiment tree never onboarded as DB metadata at all) are explicit,
     separate later decisions — not bundled into the first pass.
   - Once fully archived, the dashboard should surface archived historical
     data alongside the DB-backed experiments, and MCP tools should be able
     to give an agent access to archived logs on request (artifact URIs are
     already pointer/scheme-agnostic — a `gdrive://` scheme is already live
     for `chuk-mlx` — so this fits the existing model without a redesign).
2. **gpu-training-harness queue integration** (Task 21) — wire the harness's
   run lifecycle into this server's `/v1/queue` contract. Explicitly
   sequenced after the dashboard was fully live, which it now is.
3. **Phase 5** — pgvector hybrid search, W&B summary sync.
