from http import HTTPStatus

from starlette.requests import Request
from starlette.responses import Response

from .. import auth, service
from ..constants import DEFAULT_LIST_LIMIT, Scope
from ..models import QueueClaimRequest
from ..server import mcp
from ._shared import _ok, _parse_limit, _with_error_handling


@mcp.endpoint("/v1/queue", methods=["GET"])
@_with_error_handling
async def queue_peek(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    params = request.query_params
    max_seconds = params.get("max_seconds")
    return _ok(
        await service.peek_queue(
            backend=params.get("backend"),
            max_seconds=int(max_seconds) if max_seconds is not None else None,
            limit=_parse_limit(params.get("limit"), DEFAULT_LIST_LIMIT),
        )
    )


@mcp.endpoint("/v1/queue/claim", methods=["POST"])
@_with_error_handling
async def queue_claim(request: Request) -> Response:
    key = await auth.require_scope_from_request(request, Scope.WRITE)
    data = QueueClaimRequest.model_validate(await request.json())
    claimed = await service.claim_queue(data.backend, data.session_seconds, key.name, data.lease_seconds)
    return _ok(claimed, status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/queue/sweep", methods=["POST"])
@_with_error_handling
async def queue_sweep(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.ADMIN)
    return _ok(await service.sweep_expired_leases())
