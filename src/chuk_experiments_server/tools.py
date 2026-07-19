"""MCP surface (spec §5). Every tool forwards to this server's own REST API
(see internal_client.py) using the *calling agent's own bearer token* —
extracted from the ambient MCP context via auth.bearer_from_mcp_context() —
so the REST layer performs the exact same scope check it would for any
other client. tools.py holds no auth/validation logic of its own; it's a
thin MCP-to-REST adapter, one level further out than "the MCP server is a
thin layer over the same service functions" (the original spec's phrasing)
— now it's a thin layer over the same REST API instead, so the UI, MCP
agents, and any external REST client all go through one code path.

A tool never raises on a failed request — it returns whatever JSON body the
REST endpoint produced (its own error shape included), so a failed lookup
reads as data to the calling agent rather than an opaque tool-call failure.
"""

from typing import Any

import httpx

from . import auth, external_refs, internal_client
from .constants import DEFAULT_LIST_LIMIT, DEFAULT_SEARCH_LIMIT
from .server import mcp


async def _api_request(method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
    """Forward to `path` on this server's own REST API, using the calling
    agent's own bearer token. Never raises — a transport-level failure
    (the internal loopback call itself failing) becomes an error dict, same
    shape as errors.error_payload produces for REST/other tools."""
    token = auth.bearer_from_mcp_context()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = await internal_client.get_client().request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        return {"error": f"internal_request_failed: {exc}"}
    try:
        return resp.json()
    except ValueError:
        return {"error": "internal_response_not_json"}


def _query_params(**kwargs: Any) -> dict[str, Any]:
    """Drop None values — httpx would otherwise send them as the literal
    string 'None' in the query string."""
    return {k: v for k, v in kwargs.items() if v is not None}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool
async def get_index() -> Any:
    """The primary discovery tool — the entire compact catalogue in one call:
    slug, title, tags, status, hypothesis, and headline metric per
    experiment. Small enough to read in full and match semantically
    yourself; try this before search_experiments, and try 2-3 phrasings of
    search_experiments before concluding something doesn't exist.
    """
    return await _api_request("GET", "/v1/index")


@mcp.tool
async def list_programmes() -> Any:
    """Enumerate every research programme (e.g. cn, div, larql) with its experiment count."""
    return await _api_request("GET", "/v1/programmes")


@mcp.tool
async def list_experiments(
    programme: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    q: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> Any:
    """Browse experiments, optionally filtered by programme slug, status, tags, or full-text query.

    Args:
        programme: Programme slug to filter by (e.g. "cn")
        status: Experiment status to filter by (draft/planned/running/completed/abandoned/superseded)
        tags: Only experiments with at least one of these tags
        q: Free-text search over title/hypothesis/write-up
        limit: Maximum rows to return
    """
    params = _query_params(programme=programme, status=status, q=q, limit=limit)
    if tags:
        params["tag"] = tags
    return await _api_request("GET", "/v1/experiments", params=params)


@mcp.tool
async def get_experiment(slug: str) -> Any:
    """Fetch the full record for one experiment: hypothesis, design, latest write-up, and its runs.

    Args:
        slug: Experiment slug (e.g. "cn-7")
    """
    return await _api_request("GET", f"/v1/experiments/{slug}")


@mcp.tool
async def search_experiments(
    query: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> Any:
    """Full-text search over titles/hypotheses/write-ups, combinable with structured filters.

    Args:
        query: Free-text search query
        filters: Optional dict — programme, status, tags (list), config_key +
            config_value (matches a JSONB key on any of the experiment's
            runs), metric + metric_op + metric_value (matches a result value
            on any run, e.g. {"metric": "gsm8k_acc", "metric_op": "gt",
            "metric_value": 0.4}; metric_op is one of eq/ne/gt/gte/lt/lte)
        limit: Maximum rows to return
    """
    filters = filters or {}
    params = _query_params(
        q=query, programme=filters.get("programme"), status=filters.get("status"), limit=limit
    )
    if filters.get("tags"):
        params["tag"] = filters["tags"]
    if filters.get("config_key") and filters.get("config_value") is not None:
        params[f"config.{filters['config_key']}"] = filters["config_value"]
    if filters.get("metric") and filters.get("metric_op") and filters.get("metric_value") is not None:
        params["metric"] = filters["metric"]
        params["op"] = filters["metric_op"]
        params["value"] = filters["metric_value"]
    return await _api_request("GET", "/v1/search", params=params)


@mcp.tool
async def get_run(run_id: str) -> Any:
    """Fetch one run's detail: config, W&B URL, results, and registered artifacts.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
    """
    return await _api_request("GET", f"/v1/runs/{run_id}")


@mcp.tool
async def compare_runs(run_ids: list[str], metric: str) -> Any:
    """Tabular comparison of a single named metric across several runs.

    Args:
        run_ids: Run ids to compare
        metric: Result name to compare (e.g. "gsm8k_acc")
    """
    return await _api_request("GET", "/v1/runs/compare", params={"ids": run_ids, "metric": metric})


@mcp.tool
async def find_checkpoints(
    experiment: str | None = None,
    model: str | None = None,
    kind: str | None = None,
) -> Any:
    """Locate artifacts by experiment slug, model, and/or kind.

    Args:
        experiment: Experiment slug to filter by
        model: Model name to filter by (matches run config or experiment design)
        kind: Artifact kind (checkpoint/log/dataset/figure/tensor/other)
    """
    params = _query_params(experiment=experiment, model=model, kind=kind)
    return await _api_request("GET", "/v1/artifacts", params=params)


@mcp.tool
async def peek_queue(backend: str | None = None) -> Any:
    """Preview ready-to-claim runs (queued, dependencies satisfied) without claiming them.

    Args:
        backend: Only runs whose requirements accept this backend (or 'any'/unset)
    """
    return await _api_request("GET", "/v1/queue", params=_query_params(backend=backend))


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@mcp.tool
async def create_experiment(
    programme: str,
    title: str,
    slug: str | None = None,
    hypothesis: str | None = None,
    design: dict[str, Any] | None = None,
) -> Any:
    """Register a new planned experiment.

    Args:
        programme: Programme slug this experiment belongs to
        title: Short human-readable title
        slug: Unique experiment slug (e.g. "cn-11") — auto-generated
            (e.g. "EXP-20260718-160217-00397") when omitted
        hypothesis: What we expect and why
        design: Model/dataset/params/arms as a JSON object
    """
    body = {
        "programme": programme,
        "slug": slug,
        "title": title,
        "hypothesis": hypothesis,
        "design": design or {},
    }
    return await _api_request("POST", "/v1/experiments", json=body)


@mcp.tool
async def update_experiment_status(slug: str, status: str, tags: list[str] | None = None) -> Any:
    """Update an experiment's own lifecycle status — separate from any of
    its runs' statuses, and nothing flips it automatically: an experiment
    stays "planned" forever unless something calls this, even while its
    runs are actively running or completed. Call this when real work
    starts (-> "running") and when it wraps up (-> "completed"), the same
    way set_run_status keeps a run's own status current.

    Args:
        slug: Experiment slug (e.g. "cn-7")
        status: draft/planned/running/completed/abandoned/superseded
        tags: Optional replacement tag list (omit to leave tags unchanged)
    """
    body: dict[str, Any] = {"status": status}
    if tags is not None:
        body["tags"] = tags
    return await _api_request("PATCH", f"/v1/experiments/{slug}", json=body)


@mcp.tool
async def append_writeup(slug: str, body_md: str) -> Any:
    """Append a new write-up version to an experiment (author is the calling API key's identity).

    Args:
        slug: Experiment slug
        body_md: Full write-up body in markdown
    """
    return await _api_request("POST", f"/v1/experiments/{slug}/writeups", json={"body_md": body_md})


@mcp.tool
async def submit_result(
    run_id: str,
    name: str,
    value: float | None = None,
    verdict: str | None = None,
    notes: str | None = None,
) -> Any:
    """Submit a named metric/verdict for a run (submitted_by is the calling API key's identity).

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        name: Metric name (e.g. "val_loss_final")
        value: Scalar metric value
        verdict: pass/fail/inconclusive/n/a
        notes: Free-text notes
    """
    body = {"name": name, "value": value, "verdict": verdict, "notes": notes}
    return await _api_request("POST", f"/v1/runs/{run_id}/results", json=body)


@mcp.tool
async def register_artifact(
    run_id: str,
    kind: str,
    uri: str,
    sha256: str | None = None,
    name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Record an artifact pointer (checkpoint/log/dataset/figure/tensor) for a run.

    A run typically accumulates several kinds of artifact over its life: the
    harness/code that ran (custom, or a standard one you already know how to
    invoke), its input files (datasets, configs), its output files (logs,
    reports, metrics), and the write-up at the end. Temp/scratch files used
    only during execution generally aren't worth registering at all — only
    things someone (human or agent) might later need to fetch back.

    uri MUST already be a real, reachable location — s3://, gdrive://, or
    https://. NEVER a local file:// path or bare filesystem path: nobody
    else (not this dashboard, not a future agent, not you in a new session)
    can resolve a path on your own machine. If you have local file bytes to
    attach, call upload_artifact_to_drive instead — it uploads the content
    and registers the resulting gdrive:// artifact in one step. For large
    files (checkpoints, multi-MB+), use the presign flow
    (POST /v1/runs/{run_id}/artifacts/presign) instead of either — bytes
    should go straight to R2, not through this server.

    A checkpoint already sitting in another project's own storage (e.g.
    gpu-training-harness's s3://chuk-train/...) should just be linked here
    via this uri, not re-uploaded — this call only ever records a pointer.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        kind: Artifact kind (checkpoint/log/dataset/figure/tensor/other)
        uri: Storage URI already reachable — s3://..., gdrive://..., or https://...
        sha256: Content hash, if known — enables lineage/dedup lookups when name is also given
        name: Logical name grouping this content across runs (e.g. "v11-tokenizer"),
            for get_artifact_lineage/pins — omit for a one-off pointer with no reuse story
        meta: Additional metadata (step, epoch, format, ...)
    """
    body = {"kind": kind, "uri": uri, "sha256": sha256, "name": name, "meta": meta or {}}
    return await _api_request("POST", f"/v1/runs/{run_id}/artifacts", json=body)


@mcp.tool
async def upload_artifact_to_drive(
    run_id: str,
    filename: str,
    kind: str,
    name: str,
    content_base64: str,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Upload local file content straight to Google Drive and register the
    resulting gdrive:// artifact for a run, in one step.

    IMPORTANT — content_base64 is an MCP tool argument, which means YOU (the
    calling model) must emit the entire base64 string as literal text to
    make this call. For anything beyond a trivial size (a short config
    snippet, a few hundred bytes) that floods your own context/transcript
    for no reason. If you have shell access, prefer:
        curl -X POST <base_url>/v1/runs/{run_id}/artifacts/upload-raw \
          -H "Authorization: Bearer <key>" \
          -F "file=@<local_path>" -F "name=<name>" -F "kind=<kind>"
    which streams the file straight from disk over the network — only the
    short JSON response ever reaches your context, regardless of file
    size, and it needs nothing installed beyond curl. Reach for this tool
    only when you already have the bytes in-context anyway (e.g. content
    you just generated) and it's genuinely small.

    Content-addressed by (name, sha256 of the bytes): if this exact content
    was already uploaded under this name by an earlier run, that upload is
    reused instead of uploading again — register a harness/dataset under
    the same name every time you use it (e.g. "tok-v12-harness"), and it
    only gets stored once no matter how many runs reference it (same dedup
    behavior via the curl route above). Check get_artifact_lineage on the
    returned artifact id to see every run that has used a given piece of
    content.

    Not for multi-MB+ checkpoints either way — those should go through the
    R2 presign flow instead (POST /v1/runs/{run_id}/artifacts/presign),
    which never routes bytes through this server at all.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        filename: Name to give the file in Drive (e.g. "tokenizer_bench.py")
        kind: Artifact kind (checkpoint/log/dataset/figure/tensor/other)
        name: Logical name for dedup/lineage (e.g. "tok-v12-harness") — reuse the
            same name every time this exact content might recur across runs
        content_base64: The file's raw bytes, base64-encoded
        meta: Additional metadata (step, format, ...)
    """
    body = {
        "filename": filename,
        "kind": kind,
        "name": name,
        "content_base64": content_base64,
        "meta": meta or {},
    }
    return await _api_request("POST", f"/v1/runs/{run_id}/artifacts/upload", json=body)


@mcp.tool
async def upload_artifacts_batch(run_id: str, items: list[dict[str, Any]]) -> Any:
    """Upload several files to Google Drive and register them as artifacts
    for a run in one call — use this instead of calling
    upload_artifact_to_drive once per file when you have more than one file
    ready at the same time (e.g. a harness script plus its canonicalizer).
    Each item dedups independently by (name, sha256), including against an
    earlier item in the same batch.

    Same caution as upload_artifact_to_drive applies, multiplied by item
    count: every item's content_base64 is emitted as literal text by you,
    the calling model. For real files on disk, issue one
    `curl -F file=@path ... /artifacts/upload-raw` call per file instead
    (see upload_artifact_to_drive's docstring) — a few small curl calls
    cost you far less context than one batch call carrying several files'
    worth of base64.

    All items are validated before anything is uploaded — one bad item
    fails the whole batch rather than leaving some files stored and others
    missing.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        items: One dict per file, each with the same shape as
            upload_artifact_to_drive's arguments:
            filename, kind, name, content_base64, and optionally meta.

    Returns a list of created artifacts, in the same order as items.
    """
    return await _api_request("POST", f"/v1/runs/{run_id}/artifacts/upload-batch", json={"items": items})


@mcp.tool
async def register_git_artifact(
    run_id: str,
    owner: str,
    repo: str,
    commit: str,
    kind: str = "other",
    name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Record that a run's harness/code IS a git commit — for when the code
    already lives in a GitHub repo, so there's no reason to re-upload it as
    a Drive file. Registers `git+https://github.com/{owner}/{repo}@{commit}`
    (no bytes ever move) with `meta.git_repo`/`meta.git_commit` set for the
    dashboard, matching what you'd get from `git rev-parse HEAD` and your
    remote's owner/repo in the harness's own working directory.

    Call verify_artifact on the returned id any time you want to confirm
    the commit still actually exists on GitHub (e.g. before trusting it as
    a citation) rather than assuming registration alone means it's real.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        owner: GitHub org/user (e.g. "chrishayuk")
        repo: Repo name (e.g. "chuk-mlx")
        commit: Full commit SHA the harness ran at
        kind: Artifact kind (checkpoint/log/dataset/figure/tensor/other) — usually "other" for code
        name: Logical name for dedup/lineage across runs (e.g. "tok-v12-harness")
        meta: Additional metadata — git_repo/git_commit are always set from
            owner/repo/commit and win over any caller-supplied values of the same keys
    """
    uri = external_refs.build_git_uri(owner, repo, commit)
    computed_meta = {**(meta or {}), "git_repo": f"{owner}/{repo}", "git_commit": commit}
    body = {"kind": kind, "uri": uri, "name": name, "meta": computed_meta}
    return await _api_request("POST", f"/v1/runs/{run_id}/artifacts", json=body)


@mcp.tool
async def register_hf_artifact(
    run_id: str,
    repo_id: str,
    revision: str = "main",
    repo_type: str = "model",
    kind: str = "other",
    bytes: int | None = None,
    name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Record that a run's checkpoint/dataset IS already a Hugging Face Hub
    repo — for when the artifact already lives on the Hub, so there's no
    reason to re-upload it. Registers `hf://{repo_type}/{repo_id}@{revision}`
    (no bytes ever move) with `meta.hf_repo_id`/`meta.hf_revision`/
    `meta.hf_repo_type` set for the dashboard.

    Pass bytes (the total expected size of the repo at this revision, if
    you know it) to make verify_artifact's check meaningful beyond "the
    revision exists" — a 2026-07-19 disk-reclaim audit found an HF repo
    that matched by name but was missing 93% of its actual content (2.6GB
    of an expected 36.5GB); only a real size check caught it, not the fact
    the repo/revision existed.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        repo_id: Hub repo id (e.g. "chrishayuk/granite-4.1-3b-q4k-vindex")
        revision: Branch/tag/commit on the Hub (default "main")
        repo_type: "model" or "dataset"
        kind: Artifact kind (checkpoint/log/dataset/figure/tensor/other) — usually "checkpoint" or "dataset"
        bytes: Expected total size in bytes, if known — enables verify_artifact's
            completeness check instead of existence-only
        name: Logical name for dedup/lineage across runs
        meta: Additional metadata — hf_repo_id/hf_revision/hf_repo_type are
            always set from repo_id/revision/repo_type and win over any
            caller-supplied values of the same keys
    """
    uri = external_refs.build_hf_uri(repo_type, repo_id, revision)
    computed_meta = {
        **(meta or {}),
        "hf_repo_id": repo_id,
        "hf_revision": revision,
        "hf_repo_type": repo_type,
    }
    body = {"kind": kind, "uri": uri, "bytes": bytes, "name": name, "meta": computed_meta}
    return await _api_request("POST", f"/v1/runs/{run_id}/artifacts", json=body)


@mcp.tool
async def verify_artifact(artifact_id: int) -> Any:
    """Live-check that a git+/hf:// reference artifact (from
    register_git_artifact/register_hf_artifact) still actually resolves —
    the commit/revision exists, and for hf:// with a recorded expected
    size, the real content is actually complete. Not just "was this
    well-formed at registration time": repos get deleted, revisions get
    force-pushed away, uploads get abandoned partway through. Result is
    cached (verify_status/verified_at/verify_detail on the artifact), not
    re-checked on every read, since GitHub's unauthenticated API is capped
    at 60 requests/hour.

    Args:
        artifact_id: Artifact id (from register_git_artifact/register_hf_artifact's response)
    """
    return await _api_request("POST", f"/v1/artifacts/{artifact_id}/verify")


@mcp.tool
async def list_external_ref_artifacts(limit: int | None = None, offset: int | None = None) -> Any:
    """Every git+/hf:// reference artifact across all experiments — unlike
    get_run/get_experiment, which only ever show one run's artifacts, this
    is the whole-system view: what does this server currently point at on
    GitHub/Hugging Face, and (via each row's verify_status/verified_at)
    which of those references have actually been checked recently, and
    which came back missing/unverifiable.

    Args:
        limit: Max rows to return (default 50, capped at 500)
        offset: Rows to skip, for paging
    """
    params = _query_params(limit=limit, offset=offset)
    return await _api_request("GET", "/v1/artifacts/external-refs", params=params)


@mcp.tool
async def get_artifact_lineage(artifact_id: int) -> Any:
    """Which run produced this artifact's content, and which other runs have
    since reused it (a dedup hit via upload_artifact_to_drive) — falls out
    of grouping by (name, sha256), so this only returns something useful
    for artifacts registered with a name.

    Args:
        artifact_id: Artifact id (from register_artifact/upload_artifact_to_drive's response)
    """
    return await _api_request("GET", f"/v1/artifacts/{artifact_id}/lineage")


@mcp.tool
async def set_pin(name: str, artifact_id: int) -> Any:
    """Point a named, repointable alias (e.g. "tok-v12-tokenizer:latest" or
    ":best") at a specific artifact — creates the pin if it doesn't exist
    yet, or repoints it if it does. Use get_pin to resolve a pin back to
    its current artifact.

    Args:
        name: Pin name (any string you choose, e.g. "tok-v12-tokenizer:latest")
        artifact_id: The artifact this pin should point at right now
    """
    return await _api_request("PUT", f"/v1/pins/{name}", json={"artifact_id": artifact_id})


@mcp.tool
async def get_pin(name: str) -> Any:
    """Resolve a named pin to its current artifact.

    Args:
        name: Pin name (e.g. "tok-v12-tokenizer:latest")
    """
    return await _api_request("GET", f"/v1/pins/{name}")


@mcp.tool
async def set_run_status(run_id: str, status: str) -> Any:
    """Update a run's lifecycle status.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        status: queued/claimed/running/completed/failed/killed/lost/cancelled
    """
    return await _api_request("PATCH", f"/v1/runs/{run_id}", json={"status": status})


@mcp.tool
async def enqueue_run(
    slug: str,
    workspec: dict[str, Any],
    requirements: dict[str, Any] | None = None,
    priority: int = 0,
    depends_on: list[str] | None = None,
    est_seconds: int | None = None,
) -> Any:
    """Enqueue a run with a self-contained workspec for a harness worker to execute.

    Args:
        slug: Experiment slug this run belongs to
        workspec: Everything a worker needs to run with no other context —
            code (repo/ref/entrypoint), image, env (secret refs, not values),
            inputs, outputs, optional success expression
        requirements: e.g. {"backend": "any|colab|vastai|...", "gpu": "...", "min_vram_gb": ...}
        priority: Higher claims first
        depends_on: Run ids that must reach 'completed' before this one is ready
        est_seconds: Estimated wall-clock cost, used for session packing at claim time
    """
    body = {
        "workspec": workspec,
        "requirements": requirements or {},
        "priority": priority,
        "depends_on": depends_on or [],
        "est_seconds": est_seconds,
    }
    return await _api_request("POST", f"/v1/experiments/{slug}/runs", json=body)


@mcp.tool
async def cancel_run(run_id: str) -> Any:
    """Cancel a queued or claimed run (no-op error if it's already running/finished).

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
    """
    return await _api_request("POST", f"/v1/runs/{run_id}/cancel")
