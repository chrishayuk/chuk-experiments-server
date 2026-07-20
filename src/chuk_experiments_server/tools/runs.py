from typing import Any

from ..server import mcp
from ._shared import _api_request


@mcp.tool
async def get_run(run_id: str, summary: bool = False) -> Any:
    """Fetch one run's detail: config, W&B URL, results, and registered artifacts.

    A run with many long result notes can run to ~15K tokens, most of which
    is rarely what a caller actually needs. Pass summary=True to elide each
    result's `notes` (keeping name/value/value_json/verdict/superseded_by —
    the queryable/comparable parts) when you just need to know what was
    measured, not the full interpretation; call again without summary, or
    look at a specific result, when you need the prose too.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        summary: Elide result notes to shrink the response
    """
    data = await _api_request("GET", f"/v1/runs/{run_id}")
    if summary and isinstance(data, dict) and isinstance(data.get("results"), list):
        data["results"] = [{**r, "notes": None} for r in data["results"]]
    return data


@mcp.tool
async def compare_runs(run_ids: list[str], metric: str) -> Any:
    """Tabular comparison of a single named metric across several runs.

    Returns one row per run id given. Check `found` on each row before
    trusting `value`/`value_json`/`verdict`: `found: false` means that run
    has no current result named `metric` at all — distinct from `found:
    true` with a genuinely null value/verdict. A superseded result (see
    submit_result/mark_result_superseded) is never returned here; only the
    current, corrected value is. If the returned list is empty entirely
    (not just all-`found: false`), none of the given run_ids exist.

    Args:
        run_ids: Run ids to compare
        metric: Result name to compare (e.g. "gsm8k_acc")
    """
    return await _api_request("GET", "/v1/runs/compare", params={"ids": run_ids, "metric": metric})


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
