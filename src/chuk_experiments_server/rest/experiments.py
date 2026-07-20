from http import HTTPStatus

from starlette.requests import Request
from starlette.responses import Response

from .. import auth, service
from ..constants import DEFAULT_EXPERIMENT_ORDER, DEFAULT_EXPERIMENT_SORT, DEFAULT_LIST_LIMIT, Scope
from ..models import ExperimentCreate, ExperimentUpdate, RunCreate, WriteupCreate
from ..server import mcp
from ._shared import _ok, _parse_limit, _parse_offset, _with_error_handling


@mcp.endpoint("/v1/experiments", methods=["GET", "POST"])
@_with_error_handling
async def experiments_collection(request: Request) -> Response:
    if request.method == "GET":
        await auth.require_scope_from_request(request, Scope.READ)
        params = request.query_params
        experiments = await service.list_experiments(
            programme=params.get("programme"),
            status=params.get("status"),
            tags=params.getlist("tag") or None,
            q=params.get("q"),
            needs_conclusion=params.get("needs_conclusion") == "true",
            needs_next_action=params.get("needs_next_action") == "true",
            limit=_parse_limit(params.get("limit"), DEFAULT_LIST_LIMIT),
            offset=_parse_offset(params.get("offset")),
            sort=params.get("sort", DEFAULT_EXPERIMENT_SORT),
            order=params.get("order", DEFAULT_EXPERIMENT_ORDER),
        )
        return _ok(experiments)

    await auth.require_scope_from_request(request, Scope.WRITE)
    data = ExperimentCreate.model_validate(await request.json())
    return _ok(await service.create_experiment(data), status=HTTPStatus.CREATED)


# /v1/experiments/health must be registered before /v1/experiments/{slug} —
# routes are matched in registration order, so "health" would otherwise be
# swallowed as a slug (same gotcha /v1/runs/compare has against {run_id}).
@mcp.endpoint("/v1/experiments/health", methods=["GET"])
@_with_error_handling
async def experiments_health(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    return _ok(await service.get_research_health())


@mcp.endpoint("/v1/experiments/{slug}", methods=["GET", "PATCH"])
@_with_error_handling
async def experiment_detail(request: Request) -> Response:
    slug = request.path_params["slug"]
    if request.method == "GET":
        await auth.require_scope_from_request(request, Scope.READ)
        return _ok(await service.get_experiment(slug))

    await auth.require_scope_from_request(request, Scope.WRITE)
    data = ExperimentUpdate.model_validate(await request.json())
    return _ok(await service.update_experiment(slug, data))


@mcp.endpoint("/v1/experiments/{slug}/writeups", methods=["POST"])
@_with_error_handling
async def experiment_writeups(request: Request) -> Response:
    key = await auth.require_scope_from_request(request, Scope.WRITE)
    slug = request.path_params["slug"]
    data = WriteupCreate.model_validate(await request.json())
    return _ok(await service.append_writeup(slug, key.name, data), status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/experiments/{slug}/runs", methods=["POST"])
@_with_error_handling
async def experiment_runs(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.WRITE)
    slug = request.path_params["slug"]
    body = await request.json()
    body["experiment"] = slug
    data = RunCreate.model_validate(body)
    return _ok(await service.enqueue_run(data), status=HTTPStatus.CREATED)
