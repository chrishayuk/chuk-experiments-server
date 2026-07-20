from http import HTTPStatus

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import auth, service
from ..constants import DEFAULT_LIST_LIMIT, Scope
from ..models import (
    ArtifactCreate,
    GitArtifactCreate,
    HfArtifactCreate,
    LeaseRenewal,
    ResultCreate,
    RunUpdate,
)
from ..server import mcp
from ._shared import _ok, _parse_limit, _with_error_handling

# /v1/runs/compare must be registered before /v1/runs/{run_id} — routes are
# matched in registration order, and since run_id has no type converter
# (plain string ids now), "compare" would otherwise match {run_id} first.


@mcp.endpoint("/v1/runs/compare", methods=["GET"])
@_with_error_handling
async def runs_compare(request: Request) -> Response:
    params = request.query_params
    await auth.require_scope_from_request(request, Scope.READ)
    metric = params.get("metric")
    run_ids = params.getlist("ids")
    if not run_ids or not metric:
        return JSONResponse(
            {"error": "provide 'ids' (repeatable) and 'metric' query parameters"},
            status_code=HTTPStatus.BAD_REQUEST.value,
        )
    return _ok(await service.compare_runs(run_ids, metric))


@mcp.endpoint("/v1/runs/{run_id}", methods=["GET", "PATCH"])
@_with_error_handling
async def run_detail(request: Request) -> Response:
    run_id = request.path_params["run_id"]
    if request.method == "GET":
        await auth.require_scope_from_request(request, Scope.READ)
        return _ok(await service.get_run(run_id))

    await auth.require_scope_from_request(request, Scope.WRITE)
    data = RunUpdate.model_validate(await request.json())
    return _ok(await service.update_run(run_id, data))


@mcp.endpoint("/v1/runs/{run_id}/lease", methods=["POST"])
@_with_error_handling
async def run_lease(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.WRITE)
    run_id = request.path_params["run_id"]
    body = await request.json() if await request.body() else {}
    data = LeaseRenewal.model_validate(body)
    return _ok(await service.renew_lease(run_id, data.lease_seconds))


@mcp.endpoint("/v1/runs/{run_id}/results", methods=["POST"])
@_with_error_handling
async def run_results(request: Request) -> Response:
    key = await auth.require_scope_from_request(request, Scope.WRITE)
    run_id = request.path_params["run_id"]
    data = ResultCreate.model_validate(await request.json())
    return _ok(await service.submit_result(run_id, key.name, data), status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/results/{result_id:int}/supersede", methods=["POST"])
@_with_error_handling
async def result_supersede(request: Request) -> Response:
    """Retroactively link an existing result as superseded — the standalone
    route for when the correction was already submitted before you realized
    the old result needed marking (submit_result's own `supersedes` param
    covers the common case of doing both at once)."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    result_id = request.path_params["result_id"]
    body = await request.json()
    superseded_by = int(body["superseded_by"])
    return _ok(await service.mark_result_superseded(result_id, superseded_by))


@mcp.endpoint("/v1/runs/{run_id}/artifacts", methods=["POST"])
@_with_error_handling
async def run_artifacts(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.WRITE)
    run_id = request.path_params["run_id"]
    data = ArtifactCreate.model_validate(await request.json())
    return _ok(await service.register_artifact(data, run_id=run_id), status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/experiments/{slug}/artifacts", methods=["POST"])
@_with_error_handling
async def experiment_artifacts(request: Request) -> Response:
    """Register a pointer artifact directly against an experiment, no run
    required — for provenance that exists before any run does (e.g. a
    pre-registration document, the paradigm case: it needs queryable
    sha256/commit lineage the moment it's written, not "eventually, once a
    run exists to attach it to")."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    slug = request.path_params["slug"]
    data = ArtifactCreate.model_validate(await request.json())
    return _ok(await service.register_artifact(data, experiment_slug=slug), status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/runs/{run_id}/artifacts/git", methods=["POST"])
@_with_error_handling
async def run_artifacts_git(request: Request) -> Response:
    """Register that a run's harness/code IS a git commit — server builds
    the git+https://... uri, no bytes move. See
    service.register_git_artifact."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    run_id = request.path_params["run_id"]
    data = GitArtifactCreate.model_validate(await request.json())
    artifact = await service.register_git_artifact(
        data.owner,
        data.repo,
        data.commit,
        kind=data.kind.value,
        name=data.name,
        meta=data.meta,
        run_id=run_id,
    )
    return _ok(artifact, status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/experiments/{slug}/artifacts/git", methods=["POST"])
@_with_error_handling
async def experiment_artifacts_git(request: Request) -> Response:
    """Same as run_artifacts_git, registered directly against an experiment
    instead of a run — see experiment_artifacts for why that exists."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    slug = request.path_params["slug"]
    data = GitArtifactCreate.model_validate(await request.json())
    artifact = await service.register_git_artifact(
        data.owner,
        data.repo,
        data.commit,
        kind=data.kind.value,
        name=data.name,
        meta=data.meta,
        experiment_slug=slug,
    )
    return _ok(artifact, status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/runs/{run_id}/artifacts/hf", methods=["POST"])
@_with_error_handling
async def run_artifacts_hf(request: Request) -> Response:
    """Register that a run's checkpoint/dataset IS already a Hugging Face
    Hub repo — server builds the hf://... uri, no bytes move. See
    service.register_hf_artifact."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    run_id = request.path_params["run_id"]
    data = HfArtifactCreate.model_validate(await request.json())
    artifact = await service.register_hf_artifact(
        data.repo_id,
        revision=data.revision,
        repo_type=data.repo_type,
        kind=data.kind.value,
        bytes=data.bytes,
        name=data.name,
        meta=data.meta,
        run_id=run_id,
    )
    return _ok(artifact, status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/experiments/{slug}/artifacts/hf", methods=["POST"])
@_with_error_handling
async def experiment_artifacts_hf(request: Request) -> Response:
    """Same as run_artifacts_hf, registered directly against an experiment
    instead of a run — see experiment_artifacts for why that exists."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    slug = request.path_params["slug"]
    data = HfArtifactCreate.model_validate(await request.json())
    artifact = await service.register_hf_artifact(
        data.repo_id,
        revision=data.revision,
        repo_type=data.repo_type,
        kind=data.kind.value,
        bytes=data.bytes,
        name=data.name,
        meta=data.meta,
        experiment_slug=slug,
    )
    return _ok(artifact, status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/runs/{run_id}/cancel", methods=["POST"])
@_with_error_handling
async def run_cancel(request: Request) -> Response:
    """Dedicated action route (rather than PATCH .../{id} with status=cancelled)
    because cancellation is guarded — only valid from queued/claimed — and a
    dedicated route keeps that guard from being bypassable via the generic
    status-setting PATCH."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    return _ok(await service.cancel_run(request.path_params["run_id"]))


@mcp.endpoint("/v1/artifacts", methods=["GET"])
@_with_error_handling
async def artifacts_collection(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    params = request.query_params
    return _ok(
        await service.find_checkpoints(
            experiment=params.get("experiment"),
            model=params.get("model"),
            kind=params.get("kind"),
            limit=_parse_limit(params.get("limit"), DEFAULT_LIST_LIMIT),
        )
    )
