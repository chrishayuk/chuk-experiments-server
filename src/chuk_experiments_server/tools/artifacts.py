from typing import Any

from ..constants import ARTIFACT_EXACTLY_ONE_PARENT_ERROR
from ..server import mcp
from ._shared import _api_request, _query_params


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


def _artifact_parent_path(
    run_id: str | None, experiment_slug: str | None, suffix: str = ""
) -> str | dict[str, Any]:
    """Resolve which REST path an artifact-registration call targets (the
    plain .../artifacts route, or .../artifacts/git or .../artifacts/hf via
    `suffix`). Returns the path string, or an error dict (never raises —
    matches every other tool in this module) if the caller gave both or
    neither parent. The same "exactly one parent" rule is independently
    enforced again by service.register_artifact, since it's reachable
    directly by any REST caller — both layers share
    ARTIFACT_EXACTLY_ONE_PARENT_ERROR so they never say it differently."""
    if (run_id is None) == (experiment_slug is None):
        return {"error": ARTIFACT_EXACTLY_ONE_PARENT_ERROR}
    if run_id is not None:
        return f"/v1/runs/{run_id}/artifacts{suffix}"
    return f"/v1/experiments/{experiment_slug}/artifacts{suffix}"


@mcp.tool
async def register_artifact(
    kind: str,
    uri: str,
    run_id: str | None = None,
    experiment_slug: str | None = None,
    sha256: str | None = None,
    name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Record an artifact pointer (checkpoint/log/dataset/figure/tensor) for
    a run — or, with no run yet, directly for an experiment.

    A run typically accumulates several kinds of artifact over its life: the
    harness/code that ran (custom, or a standard one you already know how to
    invoke), its input files (datasets, configs), its output files (logs,
    reports, metrics), and the write-up at the end. Temp/scratch files used
    only during execution generally aren't worth registering at all — only
    things someone (human or agent) might later need to fetch back.

    Give exactly one of run_id/experiment_slug. Use experiment_slug for
    provenance that exists before any run does — the paradigm case is a
    pre-registration document: it needs queryable sha256/commit lineage
    (get_artifact_lineage/verify_artifact) the moment it's written, not
    "once a run eventually exists to attach it to."

    uri MUST already be a real, reachable location — s3://, gdrive://, or
    https://. NEVER a local file:// path or bare filesystem path: nobody
    else (not this dashboard, not a future agent, not you in a new session)
    can resolve a path on your own machine. If you have local file bytes to
    attach, call upload_artifact_to_drive instead — it uploads the content
    and registers the resulting gdrive:// artifact in one step (run-scoped
    only, for now). For large files (checkpoints, multi-MB+), use the
    presign flow (POST /v1/runs/{run_id}/artifacts/presign) instead of
    either — bytes should go straight to R2, not through this server.

    A checkpoint already sitting in another project's own storage (e.g.
    gpu-training-harness's s3://chuk-train/...) should just be linked here
    via this uri, not re-uploaded — this call only ever records a pointer.

    Args:
        kind: Artifact kind (checkpoint/log/dataset/figure/tensor/other)
        uri: Storage URI already reachable — s3://..., gdrive://..., or https://...
        run_id: Run id (e.g. "RUN-20260718-160217-00397") — or experiment_slug, not both
        experiment_slug: Experiment slug (e.g. "cn-7") to attach directly, no run — or run_id, not both
        sha256: Content hash, if known — enables lineage/dedup lookups when name is also given
        name: Logical name grouping this content across runs (e.g. "v11-tokenizer"),
            for get_artifact_lineage/pins — omit for a one-off pointer with no reuse story
        meta: Additional metadata (step, epoch, format, ...)
    """
    path = _artifact_parent_path(run_id, experiment_slug)
    if isinstance(path, dict):
        return path
    body = {"kind": kind, "uri": uri, "sha256": sha256, "name": name, "meta": meta or {}}
    return await _api_request("POST", path, json=body)


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

    HARD LIMIT: content_base64 must decode to 32KB (32,768 bytes) or less —
    the server rejects anything larger with a 400. This is deliberately
    small: content_base64 is an MCP tool argument, so YOU (the calling
    model) must emit the entire base64 string as literal text to make this
    call, and it lands in your own transcript/context regardless of whether
    the upload succeeds. Don't try a large file "to see if it fits" — check
    the size first. For anything above a short generated snippet, use
    upload-raw instead:
        curl -X POST <base_url>/v1/runs/{run_id}/artifacts/upload-raw \
          -H "Authorization: Bearer $CHUK_EXPERIMENTS_API_KEY" \
          -F "file=@<local_path>" -F "name=<name>" -F "kind=<kind>"
    which streams the file straight from disk over the network — only the
    short JSON response ever reaches your context, regardless of file
    size, and it needs nothing installed beyond curl. Never paste the
    literal API key into that command either — it would show up in your
    transcript exactly like oversized base64 content would. Reference it
    via an environment variable that's already set in your shell
    (CHUK_EXPERIMENTS_API_KEY, matching gpu-training-harness's own naming
    for this same server); if none is set, ask the user to export one
    rather than typing the raw key value yourself.

    Reach for this tool only when you already have the bytes in-context
    anyway (e.g. content you just generated) and it's under the limit above.

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
        content_base64: The file's raw bytes, base64-encoded — see the hard
            limit above
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

    Same hard limit as upload_artifact_to_drive applies per item (see that
    tool's docstring for the exact number) — and like that tool, every
    item's content_base64 is emitted as literal text by you, the calling
    model, landing in your own transcript regardless of item count or
    outcome. For real files on disk, issue one
    `curl -F file=@path ... /artifacts/upload-raw` call per file instead
    (see upload_artifact_to_drive's docstring for the full command,
    including how to pass the bearer key via an environment variable
    instead of pasting it literally) — a few small curl calls cost you far
    less context than one batch call carrying several files' worth of
    base64.

    All items are validated before anything is uploaded — one bad item
    fails the whole batch rather than leaving some files stored and others
    missing.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        items: One dict per file, each with the same shape as
            upload_artifact_to_drive's arguments (including its content_base64
            size limit): filename, kind, name, content_base64, and
            optionally meta.

    Returns a list of created artifacts, in the same order as items.
    """
    return await _api_request("POST", f"/v1/runs/{run_id}/artifacts/upload-batch", json={"items": items})


@mcp.tool
async def register_git_artifact(
    owner: str,
    repo: str,
    commit: str,
    run_id: str | None = None,
    experiment_slug: str | None = None,
    kind: str = "other",
    name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Record that a run's (or experiment's) harness/code IS a git commit —
    for when the code already lives in a GitHub repo, so there's no reason
    to re-upload it as a Drive file. Registers
    `git+https://github.com/{owner}/{repo}@{commit}` (no bytes ever move)
    with `meta.git_repo`/`meta.git_commit` set for the dashboard, matching
    what you'd get from `git rev-parse HEAD` and your remote's owner/repo.

    Give exactly one of run_id/experiment_slug — use experiment_slug for a
    pre-registration document's own code/config commit, registered before
    any run exists.

    Call verify_artifact on the returned id any time you want to confirm
    the commit still actually exists on GitHub (e.g. before trusting it as
    a citation) rather than assuming registration alone means it's real.

    Args:
        owner: GitHub org/user (e.g. "chrishayuk")
        repo: Repo name (e.g. "chuk-mlx")
        commit: Full commit SHA the harness ran at
        run_id: Run id (e.g. "RUN-20260718-160217-00397") — or experiment_slug, not both
        experiment_slug: Experiment slug to attach directly, no run — or run_id, not both
        kind: Artifact kind (checkpoint/log/dataset/figure/tensor/other) — usually "other" for code
        name: Logical name for dedup/lineage across runs (e.g. "tok-v12-harness")
        meta: Additional metadata — git_repo/git_commit are always set from
            owner/repo/commit and win over any caller-supplied values of the same keys
    """
    path = _artifact_parent_path(run_id, experiment_slug, suffix="/git")
    if isinstance(path, dict):
        return path
    body = {"owner": owner, "repo": repo, "commit": commit, "kind": kind, "name": name, "meta": meta or {}}
    return await _api_request("POST", path, json=body)


@mcp.tool
async def register_hf_artifact(
    repo_id: str,
    run_id: str | None = None,
    experiment_slug: str | None = None,
    revision: str = "main",
    repo_type: str = "model",
    kind: str = "other",
    bytes: int | None = None,
    name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Record that a run's (or experiment's) checkpoint/dataset IS already a
    Hugging Face Hub repo — for when the artifact already lives on the Hub,
    so there's no reason to re-upload it. Registers
    `hf://{repo_type}/{repo_id}@{revision}` (no bytes ever move) with
    `meta.hf_repo_id`/`meta.hf_revision`/`meta.hf_repo_type` set for the
    dashboard.

    Give exactly one of run_id/experiment_slug.

    Pass bytes (the total expected size of the repo at this revision, if
    you know it) to make verify_artifact's check meaningful beyond "the
    revision exists" — a 2026-07-19 disk-reclaim audit found an HF repo
    that matched by name but was missing 93% of its actual content (2.6GB
    of an expected 36.5GB); only a real size check caught it, not the fact
    the repo/revision existed.

    Args:
        repo_id: Hub repo id (e.g. "chrishayuk/granite-4.1-3b-q4k-vindex")
        run_id: Run id (e.g. "RUN-20260718-160217-00397") — or experiment_slug, not both
        experiment_slug: Experiment slug to attach directly, no run — or run_id, not both
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
    path = _artifact_parent_path(run_id, experiment_slug, suffix="/hf")
    if isinstance(path, dict):
        return path
    body = {
        "repo_id": repo_id,
        "revision": revision,
        "repo_type": repo_type,
        "kind": kind,
        "bytes": bytes,
        "name": name,
        "meta": meta or {},
    }
    return await _api_request("POST", path, json=body)


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
