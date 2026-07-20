from typing import Any

from ..server import mcp
from ._shared import _api_request


@mcp.tool
async def submit_result(
    run_id: str,
    name: str,
    value: float | None = None,
    value_json: dict[str, Any] | None = None,
    verdict: str | None = None,
    notes: str | None = None,
    supersedes: int | None = None,
) -> Any:
    """Submit a named metric/verdict for a run (submitted_by is the calling
    API key's identity).

    Numbers belong in value/value_json, not notes — compare_runs can only
    see the former. Bad: submitting one result named
    "held_out_bpb_corrected_four_way" with value=1.0 (a placeholder) and the
    real numbers written out as prose in notes ("v11-replication 0.6846 vs
    U16 0.7058 vs U18 0.7062 vs BPE16 0.7461..."). That table is now
    invisible to compare_runs — a caller asking for "held_out_bpb" gets back
    an all-null row and no way to know the numbers exist at all. Good: submit
    one result per comparable number (name="held_out_bpb_v11_replication",
    value=0.6846; name="held_out_bpb_u16", value=0.7058; ...) — or, if they're
    genuinely one structured measurement, one result with
    value_json={"v11_replication": 0.6846, "u16": 0.7058, "u18": 0.7062,
    "bpe16": 0.7461}. Either way, save notes for interpretation ("U18 beats
    U16 because...") rather than the numbers themselves.

    If this result corrects an earlier, now-wrong one, pass
    supersedes=<that result's id> — e.g. result 1139 was contaminated and
    wrong; its correction (1141/1142) should have been submitted with
    supersedes=1139 instead of only noting the correction in prose. This
    marks 1139.superseded_by so anyone fetching it later — even in
    isolation, even by ranking on verdict — sees it's no longer current,
    instead of silently trusting a stale "pass". Use mark_result_superseded
    instead if you're linking two results that already exist, retroactively.

    Args:
        run_id: Run id (e.g. "RUN-20260718-160217-00397")
        name: Metric name (e.g. "val_loss_final")
        value: Scalar metric value
        value_json: Structured metric value (e.g. a small table/breakdown) —
            use this or value, whichever fits the shape of the number(s)
        verdict: pass/fail/inconclusive/n/a
        notes: Free-text interpretation — not where the numbers themselves go
        supersedes: id of an earlier result this one corrects, if any
    """
    body = {
        "name": name,
        "value": value,
        "value_json": value_json,
        "verdict": verdict,
        "notes": notes,
        "supersedes": supersedes,
    }
    return await _api_request("POST", f"/v1/runs/{run_id}/results", json=body)


@mcp.tool
async def mark_result_superseded(result_id: int, superseded_by: int) -> Any:
    """Retroactively mark an existing result as superseded by another —
    for when you realize an old result was wrong *after* already submitting
    its correction, rather than at submission time (use submit_result's own
    `supersedes` param for the common "submit the fix now" case instead).

    Once set, anyone fetching result_id later — via get_run, in isolation,
    or by ranking on verdict — sees it's no longer current, instead of
    silently trusting a stale pass/fail.

    Args:
        result_id: The result that is now known-wrong
        superseded_by: The result that corrects it
    """
    return await _api_request(
        "POST", f"/v1/results/{result_id}/supersede", json={"superseded_by": superseded_by}
    )
