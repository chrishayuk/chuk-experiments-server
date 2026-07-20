# Roadmap

Status of each phase from the original spec, plus the architectural
decisions made along the way that the spec didn't originally cover.

## Done

- **Phase 0/1** — Postgres schema, REST API (spec §4), MCP read/write tool
  set (spec §5), all sharing one service layer (`service/`).
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
- **REST-API-only architecture** — MCP tools (`tools/`) call this server's
  own REST API over real HTTP (`internal_client.py`, a loopback `httpx`
  client), forwarding the calling agent's own bearer token, never
  `service/` directly. The dashboard now does the same thing one layer
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
  a small slice of the 241 total, not a real audit.
- **Full artifact sweep + broader repo coverage + real HF verification**
  (2026-07-19, closes the item above) — `scripts/audit_artifacts_for_git_refs.py`
  extended from 9 to 58 known local repos (every git repo under
  `~/chris-source` with a real `github.com/chrishayuk` remote; third-party
  clones like `mlx`/`llama.cpp` deliberately excluded), and HF
  checkpoint/dataset candidates now get a real file-list-and-size diff
  against the HF Hub tree API instead of stopping at name-matching. Found
  3 more verified git+ candidates in `v-tokenizers`, a repo not previously
  covered — 0 HF checkpoint matches this pass (the remaining artifacts
  don't fuzzy-match any published `chrishayuk/*` HF repo by name). 26+3=29
  of 244 total production artifacts now on `git+`/`hf://` references; the
  other ~215 are genuinely local-only research data (checkpoints/logs/
  results never checked into any git repo), correctly left untouched.
- **Dashboard-wide "External refs" screen + MCP tool** (2026-07-19) — new
  `GET /v1/artifacts/external-refs` (+ `list_external_ref_artifacts` MCP
  tool) joins artifact → run → experiment filtered to `git+`/`hf://` uris;
  new `#/external-refs` dashboard screen shows every reference across all
  experiments with the same chip+link+verify-badge rendering as run-detail
  (factored into a shared `externalRefCell` JS helper), plus a per-page
  verified/missing/unverifiable/never-checked count. Previously a
  reference only ever rendered specially on the one run-detail page it
  belonged to — no way to browse "every git/HF reference, and which have
  gone stale" without opening runs one at a time.
- **Per-user GitHub/HF tokens** (2026-07-19) — `app_user` gets
  `github_token_encrypted`/`huggingface_token_encrypted` (Fernet, the first
  reversible secret this schema stores — `api_key` only ever stores a
  one-way hash), managed from a new "My tokens" card on the Team screen
  (`PUT`/`DELETE /v1/me/tokens/{provider}`, gated by `require_dashboard_role`
  the same way key self-service is — any signed-in user manages their own,
  no elevated role needed). `verify_artifact` now resolves the *calling
  bearer key's owning user* (`api_key.created_by_user_id`, newly exposed on
  the `ApiKey` model) and prefers that user's personal token over the
  server-wide `settings.github_token`/`huggingface_token` fallback, which
  still covers bootstrap/CI keys with no owning user. Directly motivated by
  hitting Fly's shared-egress-IP GitHub rate limit earlier the same day.
  Real bug caught and fixed along the way: the dashboard's "Re-verify"
  button (shipped earlier the same day) could never have worked from a
  real browser session — a cookie-only session only ever satisfies READ
  scope by design, and the SPA never attached a bearer token — so it was
  removed; the verify badge itself (a plain READ) still renders fine, and
  a fresh check now goes through `verify_artifact` (MCP tool or direct API
  call) instead.
- **Content-addressed dedup/lineage extended to git+/hf:// artifacts**
  (2026-07-19) — these never carry a `sha256` (the commit/revision in the
  `uri` itself is the content address), so registering the same `(name,
  uri)` twice previously just created two independent `produced` rows with
  no lineage link, unlike byte-uploads which dedup via `(name, sha256)`.
  New partial unique index (`WHERE sha256 IS NULL`) mirrors the existing
  one exactly, triggering the same catch-and-retry-as-`used` path
  `register_artifact` already had. `get_artifact_lineage` groups by
  `(name, uri)` instead when `sha256` is absent — no UI change needed,
  since the run-detail artifacts table already fetches lineage for any
  named artifact regardless of scheme. Pinning needed no change at all —
  `set_pin`/`get_pin` were already scheme-agnostic.
- **Clearer create_experiment/append_writeup docstrings** (2026-07-19) —
  real case: TOK-0's hypothesis field was an inventory of every harness
  component crammed into one run-on sentence, not a claim, with eight
  undefined acronyms before any actual idea. The old docstring gave zero
  signal for this ("hypothesis: What we expect and why" was the entire
  guidance). Rewrote both docstrings with a concrete bad/good example —
  the one thing every calling agent actually reads, every time, unlike a
  skill, which only helps if explicitly invoked.
- **`conclusion`/`next_action` fields on experiment** (2026-07-19) — the
  gap identified by a long strategic review of where this project should
  go: `hypothesis` captures the plan written before a run, but nothing
  captured what was actually concluded afterward, or what should happen
  next, so a finished experiment risked becoming "a cleaner warehouse for
  the same sprawl." Deliberately kept to the plain-text shape of
  `hypothesis` (migration 009) rather than a richer `Conclusion`
  object/verdict-enum/evidence-graph design also considered and explicitly
  rejected as more machinery than a personal research tracker needs. New
  `record_experiment_conclusion` MCP tool (separate from
  `update_experiment_status` — narrative vs. lifecycle, mirroring the
  existing `append_writeup`/`update_experiment_status` split), with a
  docstring in the same rigor as the recently-rewritten
  `create_experiment`/`append_writeup` ones: a good conclusion opens with
  the verdict in plain language, a good next action is concrete, not
  "investigate further." Overview screen gets two new clickable tiles
  ("Needs conclusion" / "Needs next action", backed by a new
  `GET /v1/experiments/health`) — a first, deliberately minimal cut at the
  "research going stale" signal the same review proposed, without building
  the fuller research-debt screen it also described.

- **Systematic hypothesis/write-up quality review across all experiments**
  (2026-07-19) — 335 of 369 experiments had a hypothesis, 198 write-ups
  across 189 experiments; the TOK-0 jargon-dump case that motivated the
  docstring fix (above) was not the only one. A full DB backup was taken
  first, since unlike the byte-verified artifact migration this pass
  involves real content judgment, not a mechanical hash match. First full
  attempt (37 batches) mostly failed — embedding the raw production DB
  credential directly in each agent prompt tripped a safety classifier on
  ~40% of them, and fragile nested-quote shell commands left most of the
  rest returning empty results; fixed by having each agent derive its own
  DB connection string at runtime (`npx neonctl connection-string`, never
  pasted into a prompt) and write a real script file instead of an inline
  multi-quoted one-liner. Second attempt reviewed all 369 experiments,
  flagging 121 for a hypothesis and/or write-up rewrite; an adversarial
  verify pass (a separate agent checking each proposal against the
  original for invented/dropped/strengthened/softened content) caught that
  only 47 (39%) were actually faithful on the first try — real failure
  modes included fabricated specifics ("e.g., 'X is the capital of Y'-style
  city facts" invented for a template the original never named), fabricated
  counts, scope creep (a tool promoted into a co-equal claimed capability),
  silently dropped `**Path:**` metadata lines, and a 317-line write-up
  truncated to a 52-line stub while calling itself a full revision. The 47
  faithful rewrites were applied immediately; the other 74 went through a
  corrective pass (each agent given the specific verifier concern and
  asked to fix exactly that, nothing else) and re-verify, landing 73/74
  faithful (one flagged "still failing" only because the verify agent left
  a boolean unset in its structured output despite writing a clearly
  positive analysis — confirmed faithful by hand and applied too). Net: 121
  of 369 experiments got a rewritten hypothesis and/or write-up, all
  content-preserving, all adversarially checked before touching production.

- **Hard size cap on base64-inline artifact uploads** (2026-07-20) — real
  incident: an agent called `upload_artifacts_batch` with a ~1.9MB file
  inlined as ~2.5MB of base64 text, bloating that agent's own context/
  transcript for no reason. The existing guardrail against exactly this was
  prose-only (`upload_artifact_to_drive`/`upload_artifacts_batch`
  docstrings "steer toward" `upload-raw`) — qualitative wording ("trivial,"
  "genuinely small," one grammatically broken sentence), no concrete
  threshold, and nothing server-side stopped it: the only size gate,
  `_MAX_UPLOAD_BYTES` (20MB), was built for "large checkpoints belong in
  R2," not "keep MCP-call payloads out of a transcript," and a 1.9MB file
  never came close to tripping it. Fixed with a second, much smaller,
  separately-enforced constant, `_MAX_INLINE_BASE64_BYTES` (32KB decoded),
  applied only to the two base64/JSON routes (`upload`, `upload-batch` —
  the ones an MCP tool call actually hits); `upload-raw` (multipart,
  streamed from disk) and the R2 presign flow are untouched and keep the
  original 20MB ceiling, since bytes never pass through a model's context
  either way there. Both MCP tool docstrings now state the exact byte
  limit instead of vague adjectives. Also fixed the same class of leak for
  *credentials*: the `upload-raw` curl example told an agent to embed the
  literal bearer key in a shell command (`-H "Authorization: Bearer
  <key>"`), which shows up in that agent's transcript exactly like
  oversized base64 would — docstrings and README.md now reference
  `$CHUK_EXPERIMENTS_API_KEY` (an environment variable, matching
  gpu-training-harness's own naming for this server) instead, with an
  explicit instruction to never paste the literal key value.

- **MCP tool hardening from a real usage walkthrough** (2026-07-20) — a
  cold-comprehension pass over 30 unfamiliar experiments plus an operational
  pass on the live v12-tokenizer programme surfaced three trust/correctness
  gaps that outrank an earlier static tool-list review, plus confirmed a
  handful of smaller ones:
  - **`result.superseded_by`** (migrations 010) — the schema's first
    self-referential FK. Real incident: result 1139 was contaminated/wrong,
    corrected by 1141/1142, but the correction existed only in prose — an
    agent fetching 1139 in isolation, or ranking by verdict, would carry
    forward a stale "pass". `submit_result` gained a `supersedes` param
    (sugar for linking at submission time) and a standalone
    `mark_result_superseded` tool/route (`POST /v1/results/{id}/supersede`)
    for retroactive linking. Surfaced on every read via `get_run`'s results
    list; `get_index`'s headline metric and `compare_runs` both now exclude
    superseded results.
  - **Structured metrics + honest `compare_runs`** — `submit_result`'s MCP
    tool was missing `value_json` entirely (despite existing on the model),
    so a four-way BPB comparison table ended up as unqueryable prose in
    `notes` — `compare_runs` returned an all-null row for it with no
    signal. Docstring rewritten with a bad/good example pair (the tok-2b
    case), matching `create_experiment`'s proven "teach the norm, not just
    the schema" pattern. `compare_runs`' query fixed to also honestly
    distinguish "no current result under this metric" (`found: false`)
    from "found, value happens to be null" — previously indistinguishable,
    and previously capable of emitting duplicate rows per run when a metric
    had been submitted more than once (exactly the corrected-result case).
  - **Explicit empty-result messages** — `search_experiments`/`peek_queue`
    used to return a bare `[]` indistinguishable between "nothing exists",
    "wrong query," and "tool failure" (hit more than once in one session,
    compounded by search being lexical-only — a semantic paraphrase
    returned nothing for content that existed). Both now wrap as `{results,
    count, message?}` via a small shared `_listing` helper.
  - **`get_index` real pagination** — `limit`/`offset` existed at the
    REST/service layer since a 2026-07-19 fix but were never wired into the
    MCP tool itself, which took zero parameters; a ~380-experiment
    catalogue blew the token limit and had to be parsed from a dumped file
    by hand. Now takes `programme`/`limit`/`offset`, reports `total`
    (via a separate count query, not `COUNT(*) OVER()`, which returns
    nothing to read on an empty page), and truncates `hypothesis` to
    ~200 chars matching `search_experiments`' existing snippet pattern.
    Docstring rewritten to drop the now-false "small enough to read in
    full" framing.
  - **Smaller confirmed items**: `list_pins` (the service/REST layer
    already existed, unused); a `get_run(summary=True)` mode eliding result
    `notes` (a single run had ballooned to ~15K tokens); `tags` added to
    `create_experiment`'s MCP tool (the REST model already accepted them —
    an MCP-surface gap, not a data-model one); and **first-class
    experiment-level artifacts** (migration 011) — `artifact.experiment_id`
    alongside `run_id` (exactly one set, DB-enforced via `(run_id IS NOT
    NULL) <> (experiment_id IS NOT NULL)`), a new `POST
    /v1/experiments/{slug}/artifacts` route, and all three registration
    tools (`register_artifact`/`register_git_artifact`/
    `register_hf_artifact`) gaining an `experiment_slug` alternative to
    `run_id` — closing the gap where a pre-registration document (the
    paradigm experiment-level artifact, since it exists before any run
    does) had no queryable provenance path at all.

  Versioned design amendments (from the earlier static review) were
  explicitly dropped after the walkthrough didn't surface them as a real
  problem. The `body_md`/`body_html` duplication fix (also from that
  review) shipped anyway, folded into this pass — cheap, and already agreed
  — even though the walkthrough itself didn't re-flag it: every write-up
  read/append path now omits `body_html` on the MCP path (the dashboard
  still gets it via REST directly, where it's actually rendered).

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

## Code quality backlog (full review, 2026-07-20)

Not a bug hunt — modularity, decoupling, file size, and magic strings/
numbers, across `service.py`/`rest.py`/`tools.py`/`models.py`/`app.html`.
Architecture itself is sound (clean layering, no circular coupling,
verified the framework's import-time `@mcp.endpoint`/`@mcp.tool`
registration doesn't block splitting any of the big files into packages).
The real theme is **duplication, not disorganization**: the same fact
stated in 2-6 places with nothing tying them together. Ordered
most-severe-first; checked off as fixed.

**Cross-layer duplication/gaps**
1. [x] The "exactly one of run_id/experiment_slug" invariant for
   `register_artifact` is implemented independently in both
   `tools.py:497` (`_artifact_parent_path`) and `service.py:888`, with
   different error text. Both layers still need to enforce it (tools.py
   must pick a URL before it can even call REST) but now share one
   `ARTIFACT_EXACTLY_ONE_PARENT_ERROR` constant, so they say it identically.
2. [x] `register_git_artifact`/`register_hf_artifact`'s URI-building +
   meta-override logic (`tools.py:706`) exists *only* in the MCP layer —
   `rest.py` never imports `external_refs`, so a REST-only caller can't
   register a git/hf artifact correctly at all. Fixed properly: promoted to
   real `service.register_git_artifact`/`register_hf_artifact` functions,
   with 4 new REST routes (`POST .../artifacts/git`, `.../artifacts/hf`,
   run- and experiment-scoped) — `tools.py`'s versions are thin wrappers
   over these again, same as everything else.

**Magic strings/numbers**
3. [x] Run-status values as raw SQL string literals (`'queued'`,
   `'claimed'`, ...) in `_READY_CLAUSE`/`claim_queue`/
   `sweep_expired_leases` (`service.py:660` on), while other functions in
   the same file correctly use `RunStatus.X.value`. Also fixed the same
   pattern for `ArtifactRole`/admin-`Scope` raw strings found alongside it.
4. [x] Hypothesis/snippet truncation length (`200`) duplicated as a bare
   literal in `search_experiments` and `get_index` (`service.py:420`),
   added in different sessions, no shared constant. Now
   `SNIPPET_TRUNCATE_CHARS` in `constants.py`.
5. [x] The 32KB inline-upload cap's value is restated as prose in six
   places (`rest.py:85` comment + four `tools.py` docstrings) instead of
   one source — the code's own comment already admits this. Promoted to a
   real importable `MAX_INLINE_BASE64_BYTES` in `constants.py`; the four
   `tools.py` mentions collapsed to one canonical statement (in
   `upload_artifact_to_drive`) plus cross-references from the rest.
6. [x] `Artifact.verify_status`/`ExternalRefSummary.verify_status`
   (`models.py:203`) typed as bare `str` instead of reusing
   `external_refs.py`'s own `Literal["verified","missing","unverifiable"]`.
   Now both fields use that same `VerifyStatus` type.
7. [x] Three Python-side constants/enums hand-copied as JS literals in
   `app.html:179` on (`STATUS_CSS_CLASS`, `ExperimentStatus`,
   `ROLE_SCOPE_CEILING`), each commented "kept in sync by hand" — even
   though `app.html` is already Jinja2-rendered server-side. Now
   server-injected as JSON from `web.py`'s `app_shell` (computed once at
   import time) — no hand-copied mirrors left. Browser-verified: all three
   render with real values and zero console errors on Overview/Team.

**Duplication**
8. [x] The `bind(value)` SQL-placeholder closure copy-pasted verbatim in 4
   functions (`service.py:176` on: `list_experiments`,
   `search_experiments`, `peek_queue`, `find_checkpoints`). Extracted to a
   shared `_QueryBuilder` class.
9. [x] The programme/status/tags WHERE-clause filter blocks duplicated
   char-for-char between `list_experiments` and `search_experiments`
   (`service.py:181`). Extracted to `_apply_experiment_filters`.
10. [x] The `result` column list hand-written 3 times with no constant
    (`service.py:562`: `get_run`, `submit_result`,
    `mark_result_superseded`) — unlike run/artifact, which each got a
    partial `_X_COLUMNS` constant. Now `_RESULT_COLUMNS`.
11. [x] The artifact column list (without `experiment_id`) duplicated as a
    raw string in 4 places (`service.py:570` on) even though
    `_ARTIFACT_COLUMNS` already exists for a near-identical variant.
    `_ARTIFACT_COLUMNS` rebuilt from a single `_ARTIFACT_COLUMN_NAMES`
    tuple, plus a new `_artifact_columns(alias)` for the JOIN-aliased case
    (`find_checkpoints`) — one source of truth for both shapes.
12. [x] `externalRefCell()`/`formatArtifactUri()` (`app.html:154`,
    `518`) are two independent implementations of the same "artifact →
    GitHub/HF link" feature, already diverged: `loadPins` uses the old one,
    every other screen uses the new one. `formatArtifactUri` deleted;
    `externalRefCell` gained a uri-regex fallback for callers with no
    `meta` (pins) so it's the one implementation everywhere. Browser-
    verified on Pins and External-refs against real seeded git+/hf://
    artifacts — links render correctly, no console errors.
13. [x] Empty-state table fallback pattern repeated 10× across `app.html`
    screens (`app.html:253` on), varying only in `colspan`/message.
    Extracted `emptyRow`/`renderRows` helpers, applied at all 10 sites.

**Long functions**
14. [x] `claim_queue` (`service.py:702`, 52 lines) mixes transaction/
    row-locking, an in-Python bin-packing algorithm, a bulk UPDATE, and a
    per-id re-fetch loop in one function — the bin-packing logic can't be
    unit-tested apart from the DB transaction around it. Extracted to pure
    `_pack_runs_by_session_budget`, with its own direct unit tests
    (no DB) alongside `claim_queue`'s existing transaction-level tests.
15. [x] `run_artifacts_upload_raw` (`rest.py:630`, 60 lines) inline-
    validates 5 independent multipart form fields, each with its own
    ad-hoc error branch, before delegating to the shared upload logic.
    Extracted `_parse_upload_raw_form`. Caught and fixed a real bug from
    this refactor before it shipped: the route decorators briefly ended up
    on the wrong function — full suite + a manual re-check confirmed it
    before moving on.

**Modularity (file size)** — lower urgency than the above; nothing's
actively breaking from size alone, but all three were confirmed as clean
splits, not spaghetti:
16. [x] `service.py` (1370 lines) mixes 8 bounded contexts (programmes,
    experiments, runs, queue, results, artifacts, users/keys, tokens) —
    its own section comments already outline the split; the cross-seam
    call graph is a DAG (2 import edges), not spaghetti. Converted to a
    `service/` package: `_shared.py` (exceptions, `_QueryBuilder`,
    `_generate_ref`), `programmes.py`, `results.py`, `users.py` (+
    per-user tokens), `artifacts.py`, `runs.py` (+ queue), `experiments.py`,
    with `__init__.py` now a thin re-export module (`from .artifacts import
    register_artifact as register_artifact, ...`) so every external caller
    (`rest.py`, `web.py`, `cli.py`, `errors.py`, tests) keeps working
    unchanged via `service.<name>` attribute access — confirmed by grepping
    every `service.` reference across `src/`/`tests/` before wiring the
    re-exports. Done as a byte-identical move first (file → package, fixed
    `.` → `..` relative imports, full suite green), then extracted one
    domain at a time in dependency order (`_shared` → `programmes`/
    `results`/`users` → `artifacts` → `runs`/`experiments`), running
    `ruff check`/`ruff format`/the full test suite after each extraction —
    439/439 passing throughout, zero behavior changes.
17. [x] `rest.py`/`tools.py` (876/897 lines) each mix ~8-9 route/tool
    groups; artifacts alone is 37%/30% of each file. Split both into
    `rest/`/`tools/` packages by the same 8 domains as `service/`
    (programmes, experiments, search/index, queue, runs, artifacts, pins,
    users — `tools/` has no `users` submodule, matching the existing
    "no MCP tool wraps dashboard user/key/token self-service" design).
    Verified before splitting that route order only matters for 2 literal-
    vs-parameterized pairs (`/v1/experiments/health` vs `/v1/experiments/
    {slug}`, `/v1/runs/compare` vs `/v1/runs/{run_id}`, per
    `chuk_mcp_server`'s registry being a plain insertion-ordered dict
    handed straight to Starlette) — both pairs land inside the same
    submodule, so cross-submodule import order in `rest/__init__.py` is
    provably irrelevant and needed no special handling; `@mcp.tool` in
    `tools/` registers by name, so no ordering constraint existed there at
    all. `rest/__init__.py`/`tools/__init__.py` re-export every route/tool
    (module-qualified access, same reasoning as `service/`); updated 3
    `tests/test_rest.py` monkeypatches that poked `rest.MAX_INLINE_BASE64_BYTES`/
    `rest._MAX_UPLOAD_BYTES` directly to target `rest.artifacts.*` instead,
    since those are module-global reads inside the moved handler functions,
    not something a re-export alone fixes. 439/439 passing, ruff clean, no
    behavior changes.
18. [x] `app.html`'s `<script>` block (625 of 777 lines, zero server-side
    templating inside it) could split into plain static `.js` files at
    zero cost to the "no build step" goal — one file currently covers 7
    independent screens plus shared utilities and the router. Split into
    `static/app-core.js` (shared utilities: `$`, `esc`, `api`, `pill`,
    `pagerHtml`, `renderKV`, ...), one file per screen (`app-overview.js`,
    `app-experiments.js` [list + detail], `app-runs.js`, `app-search.js`,
    `app-pins.js`, `app-external-refs.js`, `app-team.js`), and
    `app-router.js` (the hash router, loaded last since it references every
    `load*` function). The 3 genuinely server-injected constants
    (`STATUS_CLASS`/`EXPERIMENT_STATUSES`/`ROLE_SCOPE_CEILING`, sourced from
    Python enums via `web.py`'s `app_shell`) stay in a small inline
    `<script>` in `app.html` itself — real templating, can't move to a
    static file — with the static files loaded after it via plain
    `<script src>` tags in dependency order; classic (non-module) scripts
    share one global scope, so later files see the earlier ones' top-level
    `const`/`function` declarations with no explicit wiring needed. Added
    `web.py`'s `/static/{filename}` route: static JS content is read once
    into an in-memory `{filename: content}` dict at import time (same
    "compute once" pattern as the JSON-constants dict already there) —
    doubles as the security boundary, since a request for any filename not
    already a dict key 404s with no filesystem lookup at request time at
    all, so path traversal has nothing to reach. Added `static/*.js` to
    `[tool.setuptools.package-data]` alongside the existing
    `templates/*.html` entry; the Dockerfile needed no change since it
    already `COPY`s the whole `src/` tree rather than naming files. 439/439
    passing; manually verified in a real headless-Chromium session against
    local Postgres (Overview/Experiments/Search/Pins/External-refs/Team, all
    6 screens) — zero console/page errors, correct status-pill colors and
    tag rendering confirming the server-injected globals reach the split
    files correctly.

## Fixed (production incident, 2026-07-20)

1. **`get_experiment` 500ing for every experiment in production** — caught
   live via an agent's `get_experiment` MCP call failing with a bare
   `{"error": "internal_error"}`. Root cause: `migrations/
   011_experiment_artifacts.sql` (adding `artifact.experiment_id`) and the
   code depending on it shipped together in `fcea6cd`, but `fly deploy`
   (and CI's `deploy` job) only restarts the container — it never runs
   `migrate` against production — and the manual post-deploy `chuk-
   experiments-server migrate` step got missed. `get_experiment`'s
   experiment-level-artifacts query (`service/experiments.py`) ran `WHERE
   experiment_id = $1` unconditionally on every call, so this wasn't a
   narrow edge case: every `GET /v1/experiments/{slug}` and every MCP
   `get_experiment` call was down, including the dashboard's own
   experiment-detail pages. Fixed immediately by running `migrate` by hand
   (idempotent — applied 001-011, only 011 actually did anything).
   Structural fix, part 1: added `scripts/smoke_test.py`, a read-only
   script that exercises every column added since migration 006 against
   real production data (experiment/run/artifact/result joins, not just a
   health check), and a new `smoke-test` CI job that runs it immediately
   after `deploy` — so a missed `migrate` step now fails the deploy loudly
   within a minute instead of waiting for someone to notice a 500 in the
   wild. Uses a dedicated read-scoped API key (`ci-smoke-test`, stored as
   the `CHUK_EXPERIMENTS_SMOKE_KEY` GitHub secret) rather than the
   bootstrap admin key, matching the least-privilege pattern the rest of
   the key system already follows.

   Structural fix, part 2: that still left a step someone had to
   *remember*, which is exactly how the incident happened in the first
   place. First attempt — a separate `flyctl ssh console -C
   "chuk-experiments-server migrate"` step in the CI `deploy` job, right
   after `flyctl deploy` — shipped, then immediately failed on the very
   next deploy: `Error: app chuk-experiments-server has no started VMs`.
   This app autostops idle machines (`fly.toml`'s `min_machines_running =
   0`), and the app machine had already stopped itself again in the few
   seconds between the deploy's own health check passing and `ssh console`
   trying to attach — scale-to-zero and "SSH into the app machine
   right after deploy" are fundamentally in tension. Real fix: `fly.toml`'s
   `[deploy] release_command = "chuk-experiments-server migrate"` — Fly's
   own mechanism for exactly this, running in a dedicated ephemeral machine
   *before* the new release rolls out to the real (autostop-affected) app
   machines, and aborting the deploy outright if it fails. Strictly better
   than the CI-step approach it replaced: unaffected by autostop, needs no
   `FLY_API_TOKEN`/SSH at all (it's just part of `flyctl deploy` itself),
   and — unlike the CI step — covers a manual `fly deploy` from a local
   machine too, closing a gap the CI-only version left open. `smoke-test`
   stays as a second line of defense, for whatever `release_command` itself
   doesn't catch (e.g. it failing silently, or a schema change too
   structural to be a plain additive migration).

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
   sequenced after the dashboard was fully live, which it now is. **Push
   direction done (2026-07-20, in gpu-training-harness)**: its own
   `submit_run_from_experiment` fetches an existing `RUN-…`'s `config`/
   `workspec` from here and submits it attached, one call instead of
   re-specifying the training job by hand — see that repo's ROADMAP.md. What
   remains is **pull**: the harness itself polling `/v1/queue` and
   self-selecting eligible work, rather than being told which run to run.
3. **Phase 5** — pgvector hybrid search, W&B summary sync.

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
