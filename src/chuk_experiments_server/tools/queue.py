from typing import Any

from ..server import mcp
from ._shared import _api_request, _listing, _query_params


@mcp.tool
async def peek_queue(backend: str | None = None) -> Any:
    """Preview ready-to-claim runs (queued, dependencies satisfied) without claiming them.

    An empty result means either the queue is genuinely empty, or every
    queued run has an unmet dependency (see depends_on on the run) — not a
    tool failure.

    Args:
        backend: Only runs whose requirements accept this backend (or 'any'/unset)
    """
    runs = await _api_request("GET", "/v1/queue", params=_query_params(backend=backend))
    if not isinstance(runs, list):
        return runs
    return _listing(
        runs,
        "0 runs ready to claim — either the queue is empty, or every queued "
        "run has an unmet dependency (check depends_on).",
    )
