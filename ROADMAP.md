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
- **Phase 4** — read-only dashboard: overview, browse/filter, experiment
  detail (rendered write-up), run detail, search, artifact download.
  Google "Sign in with Google" restricted to one email, reusing
  chuk-mcp-stage's existing OAuth client. **Built, tested, deployed — not
  yet live end-to-end**: `GOOGLE_CLIENT_ID`/`SECRET`/`REDIRECT_URI`,
  `DASHBOARD_ALLOWED_EMAIL`, `SESSION_SECRET`, `INTERNAL_API_KEY` still need
  adding as Fly secrets, and the production callback URL
  (`https://chuk-experiments-server.fly.dev/auth/callback`) still needs
  registering against that OAuth client in Google Cloud Console. Safe to
  deploy without them meanwhile — `/login` degrades to a 503 "not
  configured" page rather than crashing.
- **REST-API-only architecture** — both the dashboard (`web.py`) and the
  MCP tools (`tools.py`) call this server's own REST API over real HTTP
  (`internal_client.py`, a loopback `httpx` client), never `service.py`
  directly. MCP tools forward the calling agent's own bearer token; the
  dashboard uses a fixed internal API key since the human's identity is
  already verified by Google sign-in. One code path for auth/validation
  regardless of which surface is calling.
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
- 90%+ test coverage per file (98% overall), `ruff check`/`ruff format`
  clean across the repo.

## Next

1. **Finish making the dashboard live**: add the Google/session secrets to
   Fly, register the production callback URL, verify real sign-in
   end-to-end in a browser.
2. **Google Drive archival of historical local-disk data** — scoped, not
   started. A survey found ~300GB of unmigrated data across the known
   sources, overwhelmingly dominated by `larql/output/`'s 252GB of
   per-model `.vindex`/checkpoint bundles (arguably general model storage
   rather than per-experiment artifacts, and a materially different shape
   from the other sources). Sequencing:
   - Migrate `chuk-mlx/experiments/` (~1.6G) and `chris-experiments/`
     (~19G) first — both smaller, already represented in the DB as
     experiment metadata, and clearly "per-experiment" in shape.
     Verify each is fully present in Drive, *then* reclaim that local disk
     space.
   - `larql/output/` (252G) and `cell80/experiments/` (~20G, a 5th
     experiment tree never onboarded as DB metadata at all) are explicit,
     separate later decisions — not bundled into the first pass.
   - Once live, the dashboard should surface archived historical data
     alongside the DB-backed experiments, and MCP tools should be able to
     give an agent access to archived logs on request (artifact URIs are
     already pointer/scheme-agnostic, so a `gdrive://` scheme fits the
     existing model without a redesign).
3. **gpu-training-harness queue integration** (Task 21) — wire the harness's
   run lifecycle into this server's `/v1/queue` contract.
4. **Phase 5** — pgvector hybrid search, W&B summary sync.
