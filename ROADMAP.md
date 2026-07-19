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
- **`POST /runs/{id}/artifacts/upload-raw`** — a multipart route (`curl -F
  file=@path`) for real files from a shell, added because the base64-in-JSON
  routes have a structural problem no amount of clever piping fixes: an MCP
  tool's arguments are always emitted as literal text by the calling model,
  so `content_base64` shows up in full in that model's own transcript no
  matter how it was constructed upstream — a real, felt problem once the
  v12-tokenizer harness (itself an agent framework) started uploading its
  own files mid-task. `upload-raw` streams bytes straight from disk over
  the network instead; only curl's short JSON response ever reaches the
  agent's context, and it needs nothing installed beyond curl, unlike a
  local CLI wrapper (impractical once "the servers are all remote").
  `upload_artifact_to_drive`/`upload_artifacts_batch` MCP tool docstrings
  now explicitly steer toward it for anything beyond a trivial inline size.
- **`git`/`hf` artifact reference kinds + real verification** (2026-07-19)
  — `register_git_artifact`/`register_hf_artifact` record
  `git+https://github.com/{owner}/{repo}@{commit}` /
  `hf://model|dataset/{repo_id}@{revision}` pointers, no bytes moved, on
  the existing pointer-registration path (just an extended
  `VALID_ARTIFACT_URI_PREFIXES`, no new REST route needed for
  registration). The actual point is `POST /artifacts/{id}/verify`
  (`external_refs.py`): a same-day disk-reclaim pass over `larql/output/`
  found a vindex that matched an HF repo by name but was only 2.6GB of an
  expected 36.5GB — the weight binaries were never actually uploaded.
  `verify_hf_ref` does the same file-list-and-size diff that caught it by
  hand (HF's `.../tree/{revision}?recursive=true`, summed against the
  artifact's own recorded `bytes`); `verify_git_ref` confirms a commit
  still resolves against GitHub's API (only `github.com` is checked —
  anything else comes back `unverifiable`, not a false `verified`). Both
  via plain `httpx` REST calls, no new SDK dependency. Cached
  (`verify_status`/`verified_at`/`verify_detail`), not re-checked
  automatically — GitHub's unauthenticated API is 60 req/hr. Dashboard
  renders these as a clickable chip to the real github.com/huggingface.co
  page plus a verified/missing/unverifiable badge, with re-verify firing
  only on click. First real migration pass done same day: found
  `tiny-model`'s v12-tokenizer harness code was itself uncommitted,
  committed + pushed it (`d1f39b1`), then updated 11 of 241 production
  artifacts in place (`gdrive://` → `git+`) whose recorded sha256 matched
  that commit's content exactly byte-for-byte — 5 candidates deliberately
  left untouched (2 with no recorded sha256 to verify against, 3 older
  superseded revisions with no commit to honestly point at). This was a
  narrow heuristic sweep (artifacts named like harness/script files) over
  a small slice of the 241 total, not a real audit — see item 5 below for
  the actual full sweep, not yet done.

## Fixed (found via code review, 2026-07-19)

A review of `src/chuk_experiments_server/` (not the SPA, migrations, or
scripts) turned up 7 concrete issues, all confirmed against the actual
code before fixing, each verified with a new regression test:

1. **Open redirect via `meta.drive_url`** (`rest.py`) — a WRITE-scoped
   caller could set `meta.drive_url` to an arbitrary URL, and
   `artifact_download` followed it unconditionally. Fixed two ways: the
   computed value now always wins over caller `meta` on the upload/dedup
   path, and `artifact_download` additionally validates `drive_url`
   against `TRUSTED_DRIVE_URL_PREFIX` before ever redirecting — needed
   because plain `register_artifact` still allows arbitrary `meta` by
   design (e.g. linking a checkpoint with another project's own metadata
   shape), so validation has to happen at redirect time regardless of who
   wrote it.
2. **Admin-revocation race** (`service.py:revoke_user`) — the "don't
   revoke the last admin" guard was check-then-act with no row lock.
   Fixed with `SELECT ... FOR UPDATE` over every active admin row before
   counting (Postgres rejects `FOR UPDATE` combined with an aggregate, so
   the count happens in Python over the locked rows) — a concurrent
   revoke of a different admin now blocks on the lock instead of racing a
   stale count.
3. **Dedup race breaks lineage** (`service.py`) — two simultaneous
   uploads of identical `(name, sha256)` could both miss the dedup hit
   and both insert `role='produced'`, silently dropping one from
   `get_artifact_lineage`. Fixed with a partial unique index
   (`migrations/005_artifact_produced_unique.sql`,
   `(name, sha256) WHERE role='produced' AND name IS NOT NULL`) —
   `register_artifact` catches the resulting `UniqueViolationError` and
   falls back to registering as `role='used'` instead of erroring,
   exactly what the dedup check would have found had it run a moment
   later.
4. **Unescaped Drive API query injection** (`drive_storage.py:ensure_folder`)
   — artifact `name` was interpolated directly into a Drive query string.
   Fixed with `_escape_drive_query_value`, backslash-escaping `\` and `'`
   per Drive's documented query-string convention.
5. **`limit` params unbounded, bad input → 500 not 400** — `MAX_LIST_LIMIT`
   was defined but referenced nowhere, and `?limit=abc` raised an
   uncaught `ValueError`. Fixed with shared `_parse_limit`/`_parse_offset`
   helpers — non-numeric or negative input is now a 422, and anything
   over `MAX_LIST_LIMIT` is silently clamped rather than driving an
   unbounded query.
6. **No exception logging anywhere** — `_with_error_handling` mapped
   every unmapped exception straight to `{"error": "internal_error"}`
   with no `logger.exception(...)`. Fixed by logging (with full
   traceback) specifically when `error_payload` falls through to
   `INTERNAL_SERVER_ERROR` — the expected-control-flow cases
   (`NotFoundError`/`ConflictError`/etc., which already carry their own
   4xx status) stay unlogged, so this doesn't turn into log spam.
7. **`get_index()` has no pagination** — full table scan + a correlated
   subquery per row, unbounded, on the tool documented as "most-used."
   Fixed with `limit`/`offset` (default `MAX_LIST_LIMIT`, so today's
   dataset size sees no behavior change) on both the service function and
   `GET /v1/index`.

## Next

1. **Google Drive archival of historical local-disk data** — first pass
   done, wider machine-wide sweep paused (2026-07-19). A survey found
   ~300GB of unmigrated data across the known sources, overwhelmingly
   dominated by `larql/output/`'s 252GB of per-model `.vindex`/checkpoint
   bundles (arguably general model storage rather than per-experiment
   artifacts, and a materially different shape from the other sources).
   Sequencing:
   - `chuk-mlx/experiments/` (~1.6G) — **done**: archived, verified
     (896/898 files; the 2 unarchived are `.pyc` bytecode caches, a
     deliberate exclusion), local copy reclaimed.
   - `chris-experiments/` (~19G) — **done**: archived, verified (`--verify`
     reports 4023/4023 files, byte-for-byte match against the real
     checkout), local copy reclaimed (22G → 3.3G). Root-caused a silent
     194-file gap on the way: the old catch-all only covered whole
     top-level directories with zero `INDEX.md` `Path:` references,
     missing root-level loose files and unindexed content sitting next to
     a programme dir's real (Path-referenced) experiment subdirectories
     (e.g. `grammar/data/`) — replaced with a single residual catch-all
     pass. Also found and fixed a separate bug while re-running it:
     artifact registration had no idempotency check, so 3 earlier runs had
     left 306 exact duplicate artifact rows in production (153 directories
     × 2 extra copies) — deduped directly, and the script now checks for
     an existing matching artifact before registering (confirmed: 0
     duplicate groups after this run, despite most of the 153 directories
     already being linked from earlier runs). **Near-miss during the local
     reclaim**: the "safe to delete" check (manifest-listed + git-untracked
     + size-matched-since-archival) doesn't account for the manifest having
     just been *freshly written by this same archival run* — 106 genuinely
     new, uncommitted files (not old archived data) legitimately passed all
     three gates and were deleted, then restored from Drive by drive_id
     once caught via a `git status` line-count regression. Any future
     bulk-reclaim tooling needs to snapshot the manifest (or file list)
     *before* the archival run it's auditing against, not read the
     post-run manifest as if it were a stable, independent source of truth.
   - `larql/` (406G) — partially reclaimed (2026-07-19): `target/` (153G,
     plain Cargo build cache, gitignored, rebuilds via `cargo build
     --release`) deleted outright; 2 of 17 `output/*.vindex` dirs
     (`granite-4.1-8b-q4k`, `granite-4.1-3b-q4k`, 14.6G) deleted after
     verifying file-for-file against `chrishayuk`'s HF namespace
     (`HfApi.model_info(..., files_metadata=True)`, diffing both file
     lists and sizes — not just repo-name matching). That check caught a
     third candidate, `granite-4.1-30b-q4k.vindex`, that matched an HF repo
     *by name* but wasn't actually backed up — HF had only 2.6GB of its
     36.5GB, missing the actual weight binaries (`gate_vectors.bin`,
     `interleaved_kquant.bin`). 406G → 240G. The remaining 15 `output/`
     vindex dirs (~237G) have no HF counterpart at all and stay untouched.
     This ad hoc verification is exactly what item 6 below (`git`/`hf`
     artifact source kinds + a `verify` step) should make a built-in,
     queryable server feature instead of a one-off shell/Python check.
   - Not yet surveyed, explicitly paused for now: `cell80/` (~59G, also a
     5th experiment tree never onboarded as DB metadata at all — see
     `~/chris-source/cell80/experiments/`), `chuk-ai/` (~33G),
     `gpu-training-harness/` (~13G), `chuk-speccy/` (~12G),
     `chris-pile-3/` (~9.4G), `tiny-model/` (~5G), `chuk-ai-video/` (~5G),
     `cardputer-sim/` (~4.3G), `chuk-soma/` (~2.8G),
     `chuk-robot-benches/` (~2.5G), `ffn-record/` (~2.4G), and a long tail
     of smaller (1-2G) directories. Same methodology applies: check git
     state first (several of the above are git repos that may carry
     uncommitted work, same as `chris-experiments`/`chuk-mlx` did), then
     verify against whatever remote each already uses (Drive/HF/git) before
     any local delete — never trust a naming convention alone.
   - Once fully archived, the dashboard should surface archived historical
     data alongside the DB-backed experiments, and MCP tools should be able
     to give an agent access to archived logs on request (artifact URIs are
     already pointer/scheme-agnostic — a `gdrive://` scheme is already live
     for `chuk-mlx` — so this fits the existing model without a redesign).
2. **gpu-training-harness queue integration** (Task 21) — wire the harness's
   run lifecycle into this server's `/v1/queue` contract. Explicitly
   sequenced after the dashboard was fully live, which it now is.
3. **Phase 5** — pgvector hybrid search, W&B summary sync.
4. **Full sweep of existing artifacts for git/HF migration candidates** —
   the 2026-07-19 pass (see "Done" above) only checked 16 of 241 total
   production artifacts, found by a narrow heuristic (gdrive:// artifacts
   named like harness/script files). A real sweep means going through all
   241: for each, does its `name`/`source_path`/content plausibly match a
   real git commit or an already-published HF repo? — same byte-for-byte
   verification discipline (sha256 against a real commit; file-list-and-
   size diff against a real HF revision), not name-matching. Likely
   surfaces more candidates than the harness-script slice already found
   (e.g. checkpoints that happen to already be on HF).
5. **Dashboard-wide visualization for git/HF-referenced artifacts** — today
   a git/hf artifact only renders specially (chip + link + verify badge)
   on the specific run-detail page it belongs to, or as a formatted link
   if it's pinned. There's no aggregate view at all: no way to browse
   "every artifact referencing a git repo or HF model across all
   experiments," and no overview-level count of verified vs.
   missing/unverifiable/never-checked. A dedicated screen (or a filter on
   the existing Experiments/Search views) would make "which of our
   external references have gone stale" a glanceable fact instead of
   something only discoverable by opening runs one at a time.
6. **Per-user GitHub/HF tokens** (Team screen) — `settings.github_token`/
   `huggingface_token` today are single server-wide env vars, which forces
   an awkward choice: leave unset (verify degrades to `unverifiable` under
   rate limiting) or set one broadly-scoped personal token for everyone.
   Concretely hit this 2026-07-19: migrating 11 real v12-tokenizer harness
   artifacts to `git+` references, `verify_artifact` came back
   `unverifiable` for all of them — not a bug, Fly's *shared* egress IP was
   already at GitHub's 60/hr unauthenticated limit (confirmed: the same
   commit checked fine, 51/60 remaining, from a non-Fly IP moments later).
   The right fix isn't one shared secret; it's each user storing their own
   token (encrypted at rest, same self-service model as API keys on the
   Team screen already), used for that user's own `verify_artifact` calls
   — narrower blast radius than a single broadly-scoped org-wide token,
   and no Fly secret/redeploy needed to rotate or add one.

## New features under consideration (2026-07-19)

A review of what W&B/MLflow/LangSmith-style tools do well, filtered against
a hard constraint: "experiment" here spans model training, agent behavior,
MCP tool/server behavior, and `cell80/` architecture research — not just
ML runs (see `~/chris-source/cell80/experiments/`) — so anything
ML-training-only (live loss-curve streaming, GPU-utilization panels) is
out of scope. Prioritized, starting with the trace/span tree:

1. **Trace/span tree per run** — building next. Today a run only gets flat
   `result` rows, with no record of the individual LLM calls, MCP tool
   invocations, or agent sub-steps that produced them — the single
   biggest gap versus an LLM/agent-eval tool (LangSmith/Langfuse), and
   *more* relevant to agent/MCP/cell80 experiments than to ML training: an
   agent transcript or a tool-call sequence is exactly a span tree. New
   `span` table (`run_id`, `parent_span_id`, `kind`, `input`/`output`
   JSON, timing, cost) rolls up into per-run cost/latency for free,
   replacing the single `run.cost_usd` total.
2. **Gates as a first-class concept** — the v12-tokenizer funnel's `design`
   JSON already encodes real gates (G0-G3, threshold, pass condition),
   currently buried in prose. A dedicated `gate` sub-resource (name,
   threshold, observed value, status) makes "has TOK-0 cleared G0 yet?"
   queryable and dashboard-visible instead of requiring a write-up read.
3. **Declarative multi-run submission** (generalized from W&B Sweeps —
   explicitly not porting Bayesian/GP optimization, which only pays off
   for continuous training loops). The generalizable kernel is "expand a
   parameter grid into N queued runs against one experiment," reusing the
   existing queue/lease system — a temperature x prompt-version x model
   grid for an agent eval works the same as an opcode x architecture grid
   for cell80.
4. **Promote a failed result to a regression fixture** (Braintrust-style)
   — flag a specific failing input as a permanent case auto-included in
   future comparable runs. Useful for agent/MCP behavior regressions
   specifically, not ML-specific at all.
5. **Notifications on gate pass/fail or run completion** — a webhook/Slack
   ping instead of a manual check-back on long-running async work.

(Item 6, first-class git/HF artifact reference kinds with real
verification, shipped 2026-07-19 — see "Done" above.)

Smaller, not-really-new-feature items noted alongside these: a Compare
view in the dashboard (UI only, over the existing `compare_runs`), using
this server as the persistent system of record for Lazarus/KV-Anatomist
experiments (zero new code — Lazarus's own `ExperimentStore` is in-memory
only and doesn't survive a restart), a session/agent provenance tag on
write-ups/runs, and a stage label (`:staging`/`:prod`) on `artifact_pin`.
