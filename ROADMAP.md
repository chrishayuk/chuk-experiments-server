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
- 90%+ test coverage per file (98% overall, 296 tests), `ruff check`/`ruff
  format` clean across the repo.
- **Google Drive artifact upload + sortable/filterable Experiments list** —
  `POST /runs/{id}/artifacts/upload` uploads local bytes straight to Drive
  and registers the resulting `gdrive://` artifact, for the small
  provenance/config/log/dataset files an agent has bytes for right now
  (large checkpoints still go through R2's presign flow, bytes never
  transiting this server). `register_artifact` rejects `file://` and bare
  local paths outright — the bug that prompted this whole pass: 11 real
  TOK-0 artifacts had been silently recorded as unusable local paths.
  Experiments list gained `sort`/`order` query params, clickable sortable
  column headers, and chuk-train-style instant-apply status filter chips
  (replacing a dropdown+button).
- **Content-addressed artifact dedup, lineage, and pins** — artifacts gain
  an optional `name` and a `role` (`produced`/`used`); uploading the same
  `(name, sha256)` content again (a harness/dataset reused across many
  runs) reuses the original Drive file instead of re-uploading, tagging
  the reuse `role=used`. `GET /artifacts/{id}/lineage` reports which run
  produced a piece of content and which runs have since reused it — no
  separate graph table, it falls out of grouping by `(name, sha256, role)`.
  `artifact_pin` (`migrations/004_artifact_lineage.sql`) gives W&B-style
  named, repointable aliases (`PUT /pins/{name}`, e.g.
  `"tok-v12-tokenizer:latest"`). Both are dashboard-visible: run-detail
  shows a Name column with a "used by N other runs" note and any
  "pinned as ..." badges; a new global **Pins** screen lists every pin
  with its current target (run/kind/uri). `POST .../upload-batch` (+ the
  `upload_artifacts_batch` MCP tool) uploads several files in one round
  trip instead of one MCP call per file, each item still deduping
  independently — added after real v12-tokenizer usage made the
  per-file round-trip cost (base64 transit + decode/hash/upload/insert,
  repeated per file) a real, felt friction point.
- **`update_experiment_status` MCP tool** — `set_run_status` let an agent
  keep a *run's* status current, but nothing on the MCP surface could ever
  update an *experiment's* own status (only the REST `PATCH` route
  existed). Since nothing links the two automatically, every experiment
  created via MCP was stuck at whatever status it was created with
  forever — caught because the entire v12-tokenizer programme sat at
  "planned" in the dashboard despite TOK-0 actively running.
- **Results/design readability pass** — an experiment's runs table had no
  result columns at all, and run-detail buried its own Results table below
  two raw JSON dumps (config, workspec) — the actual outcome was the least
  visible thing on either page. Experiment detail now rolls up every run's
  results (run/metric/value/verdict) right after the hypothesis; run-detail
  moves Results/Artifacts above Config/Workspec; `design`/`config`/
  `workspec` render as labeled key/value sections (`renderKV`) instead of
  one opaque JSON blob.
- Dashboard Team screen shows the server's MCP URL and a `claude mcp add`
  template, and fills in a ready-to-copy connect command (real key
  substituted in, one-click copy) the moment a new key is generated —
  there was previously no in-app guidance on how to actually connect an
  MCP client at all.

## Known issues (found via code review, 2026-07-19)

A review of `src/chuk_experiments_server/` (not the SPA, migrations, or
scripts) turned up 7 concrete issues, all confirmed against the actual
code, none needing a design call — fixing all of them next:

1. **Open redirect via `meta.drive_url`** (`rest.py`) — a WRITE-scoped
   caller can set `meta.drive_url` to an arbitrary URL (spread after the
   computed value in `_upload_or_dedup_artifact`; never validated at all
   on the plain `register_artifact` path). `artifact_download` then
   302-redirects there unconditionally — an open-redirect/phishing vector
   for anyone who opens a download link.
2. **Admin-revocation race** (`service.py:revoke_user`) — the "don't
   revoke the last admin" guard is check-then-act with no row lock; two
   concurrent revokes of two different admins can both pass the count
   check and leave zero active admins.
3. **Dedup race breaks lineage** (`service.py`) — `find_artifact_by_name_sha`
   + insert is check-then-act with no unique constraint; two simultaneous
   uploads of identical `(name, sha256)` can both miss the dedup hit and
   both insert `role='produced'`, and `get_artifact_lineage`'s `next(...)`
   silently drops one of them from lineage entirely.
4. **Unescaped Drive API query injection** (`drive_storage.py:ensure_folder`)
   — artifact `name` is interpolated directly into a Drive query string
   with no escaping; a `'` in the name breaks or reshapes the query.
5. **`limit` params unbounded, bad input → 500 not 400** — `MAX_LIST_LIMIT`
   is defined but referenced nowhere; `?limit=abc` raises an uncaught
   `ValueError` that falls through to a generic 500 instead of a 400.
6. **No exception logging anywhere** — `_with_error_handling`/`errors.py`
   map every unmapped exception to `{"error": "internal_error"}` with zero
   `logger.exception(...)` — real bugs vanish without a trace in
   production.
7. **`get_index()` has no pagination** — full table scan + a correlated
   subquery per row, unbounded, on the tool documented as "most-used."

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
     production. Root-caused a silent 194-file gap: the old catch-all only
     covered whole top-level directories with zero `INDEX.md` `Path:`
     references, missing root-level loose files and unindexed content
     sitting next to a programme dir's real (Path-referenced) experiment
     subdirectories (e.g. `grammar/data/`) — replaced with a single
     residual catch-all pass, verified byte-for-byte against the real
     checkout. Also found and fixed a separate bug while re-running it:
     artifact registration had no idempotency check, so 3 earlier runs had
     left 306 exact duplicate artifact rows in production (153 directories
     × 2 extra copies) — deduped directly, and the script now checks for
     an existing matching artifact before registering.
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
