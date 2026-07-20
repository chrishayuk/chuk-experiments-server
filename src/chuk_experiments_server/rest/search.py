from http import HTTPStatus

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import auth, service
from ..constants import DEFAULT_SEARCH_LIMIT, MAX_LIST_LIMIT, Scope
from ..server import mcp
from ._shared import _ok, _parse_limit, _parse_offset, _with_error_handling

_CONFIG_QUERY_PREFIX = "config."


@mcp.endpoint("/v1/search", methods=["GET"])
@_with_error_handling
async def search(request: Request) -> Response:
    """FTS (`q`) combinable with structured filters (spec §5a):
    `programme`, `status`, `tag` (repeatable), `metric`+`op`+`value`, and one
    `config.<key>=<value>` JSONB predicate. At least one of `q` or a filter
    must be given."""
    await auth.require_scope_from_request(request, Scope.READ)
    params = request.query_params
    query = params.get("q")

    config_key = None
    config_value = None
    for key in params.keys():
        if key.startswith(_CONFIG_QUERY_PREFIX):
            config_key = key[len(_CONFIG_QUERY_PREFIX) :]
            config_value = params[key]
            break

    metric, op, value = params.get("metric"), params.get("op"), params.get("value")
    tags = params.getlist("tag") or None

    if not any((query, params.get("programme"), params.get("status"), tags, config_key, metric)):
        return JSONResponse(
            {"error": "provide at least one of: q, programme, status, tag, config.<key>, metric+op+value"},
            status_code=HTTPStatus.BAD_REQUEST.value,
        )

    return _ok(
        await service.search_experiments(
            query=query,
            programme=params.get("programme"),
            status=params.get("status"),
            tags=tags,
            config_key=config_key,
            config_value=config_value,
            metric=metric,
            metric_op=op,
            metric_value=float(value) if value is not None else None,
            limit=_parse_limit(params.get("limit"), DEFAULT_SEARCH_LIMIT),
            offset=_parse_offset(params.get("offset")),
        )
    )


@mcp.endpoint("/v1/index", methods=["GET"])
@_with_error_handling
async def index(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    params = request.query_params
    rows, total = await service.get_index(
        limit=_parse_limit(params.get("limit"), MAX_LIST_LIMIT),
        offset=_parse_offset(params.get("offset")),
        programme=params.get("programme"),
    )
    return _ok({"results": rows, "total": total})
