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

import uuid
from typing import Any

import httpx

from . import auth, internal_client
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
async def get_run(run_id: int) -> Any:
    """Fetch one run's detail: config, W&B URL, results, and registered artifacts.

    Args:
        run_id: Numeric run id
    """
    return await _api_request("GET", f"/v1/runs/{run_id}")


@mcp.tool
async def compare_runs(run_ids: list[int], metric: str) -> Any:
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
    slug: str,
    title: str,
    hypothesis: str | None = None,
    design: dict[str, Any] | None = None,
) -> Any:
    """Register a new planned experiment.

    Args:
        programme: Programme slug this experiment belongs to
        slug: Unique experiment slug (e.g. "cn-11")
        title: Short human-readable title
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
async def append_writeup(slug: str, body_md: str) -> Any:
    """Append a new write-up version to an experiment (author is the calling API key's identity).

    Args:
        slug: Experiment slug
        body_md: Full write-up body in markdown
    """
    return await _api_request("POST", f"/v1/experiments/{slug}/writeups", json={"body_md": body_md})


@mcp.tool
async def submit_result(
    run_id: int,
    name: str,
    value: float | None = None,
    verdict: str | None = None,
    notes: str | None = None,
) -> Any:
    """Submit a named metric/verdict for a run (submitted_by is the calling API key's identity).

    Args:
        run_id: Numeric run id
        name: Metric name (e.g. "val_loss_final")
        value: Scalar metric value
        verdict: pass/fail/inconclusive/n/a
        notes: Free-text notes
    """
    body = {"name": name, "value": value, "verdict": verdict, "notes": notes}
    return await _api_request("POST", f"/v1/runs/{run_id}/results", json=body)


@mcp.tool
async def register_artifact(
    run_id: int,
    kind: str,
    uri: str,
    sha256: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Record an artifact pointer (checkpoint/log/dataset/figure/tensor) for a run.

    Args:
        run_id: Numeric run id
        kind: Artifact kind (checkpoint/log/dataset/figure/tensor/other)
        uri: Storage URI (s3://... or file://...)
        sha256: Content hash, if known
        meta: Additional metadata (step, epoch, format, ...)
    """
    body = {"kind": kind, "uri": uri, "sha256": sha256, "meta": meta or {}}
    return await _api_request("POST", f"/v1/runs/{run_id}/artifacts", json=body)


@mcp.tool
async def set_run_status(run_id: int, status: str) -> Any:
    """Update a run's lifecycle status.

    Args:
        run_id: Numeric run id
        status: queued/claimed/running/completed/failed/killed/lost/cancelled
    """
    return await _api_request("PATCH", f"/v1/runs/{run_id}", json={"status": status})


@mcp.tool
async def enqueue_run(
    slug: str,
    workspec: dict[str, Any],
    requirements: dict[str, Any] | None = None,
    priority: int = 0,
    depends_on: list[int] | None = None,
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
        "slug": f"run-{uuid.uuid4().hex[:12]}",
        "workspec": workspec,
        "requirements": requirements or {},
        "priority": priority,
        "depends_on": depends_on or [],
        "est_seconds": est_seconds,
    }
    return await _api_request("POST", f"/v1/experiments/{slug}/runs", json=body)


@mcp.tool
async def cancel_run(run_id: int) -> Any:
    """Cancel a queued or claimed run (no-op error if it's already running/finished).

    Args:
        run_id: Numeric run id
    """
    return await _api_request("POST", f"/v1/runs/{run_id}/cancel")
