"""REST surface (spec §4). Each handler: check scope, validate body into a
Pydantic model, call `service`, return the result. All error translation goes
through `errors.error_payload` so REST and MCP report failures the same way.

chuk-mcp-server's endpoint registry keys routes by path string alone (not
path+method), so two `@mcp.endpoint` calls for the same path silently
overwrite each other. Routes that need more than one HTTP method are
therefore registered ONCE with `methods=[...]` and dispatch on
`request.method` inside a single handler.
"""

import base64
import binascii
import hashlib
import json
import logging
from functools import wraps
from http import HTTPStatus
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import auth, drive_storage, service, storage
from .config import settings
from .constants import (
    DEFAULT_EXPERIMENT_ORDER,
    DEFAULT_EXPERIMENT_SORT,
    DEFAULT_LIST_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    GDRIVE_URI_PREFIX,
    GIT_URI_PREFIXES,
    HF_URI_PREFIX,
    MAX_LIST_LIMIT,
    PRESIGN_PUT_EXPIRY_SECONDS,
    TRUSTED_DRIVE_URL_PREFIX,
    ArtifactRole,
    Scope,
    TokenProvider,
)
from .errors import error_payload
from .models import (
    ApiKeyCreate,
    AppUserCreate,
    ArtifactBatchUploadRequest,
    ArtifactCreate,
    ArtifactPinSet,
    ArtifactPresignRequest,
    ArtifactUploadRequest,
    ExperimentCreate,
    ExperimentUpdate,
    LeaseRenewal,
    QueueClaimRequest,
    ResultCreate,
    RunCreate,
    RunUpdate,
    UserTokenSet,
    WriteupCreate,
)
from .serialization import to_jsonable
from .server import mcp

logger = logging.getLogger(__name__)

_R2_NOT_CONFIGURED = {"error": "not_implemented", "detail": "R2 is not configured on this server"}
_DRIVE_NOT_CONFIGURED = {
    "error": "not_implemented",
    "detail": "Google Drive is not configured on this server",
}
#: Small provenance/config/log/dataset files only — content travels through
#: this server as base64 in the request body, unlike R2's presign flow
#: (bytes never transit the server at all). Large files belong in R2.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: Hard cap for the JSON/base64 routes specifically (upload, upload-batch —
#: the ones an MCP tool call hits, where content_base64 is a literal
#: tool-call argument the calling model must emit as text, landing in that
#: model's own transcript regardless of outcome). Kept small on purpose so an
#: oversized call is rejected deterministically instead of relying on the
#: model to judge "small enough" for itself. upload-raw (multipart, streamed
#: from disk) and the R2 presign flow never route bytes through a model's
#: context either way, so they keep the much larger _MAX_UPLOAD_BYTES ceiling
#: instead. If this changes, update the matching text in tools.py's
#: upload_artifact_to_drive/upload_artifacts_batch docstrings too.
_MAX_INLINE_BASE64_BYTES = 32 * 1024


def _ok(data: Any, status: HTTPStatus = HTTPStatus.OK) -> JSONResponse:
    return JSONResponse(to_jsonable(data), status_code=status.value)


def _parse_limit(raw: str | None, default: int) -> int:
    """A bad `?limit=` (non-numeric, negative) is a 422, not an uncaught
    ValueError falling through to a generic 500 — and a huge one is
    silently clamped to MAX_LIST_LIMIT rather than driving an unbounded
    query as the table grows."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise service.ValidationError(f"limit must be an integer, got '{raw}'") from None
    if value < 0:
        raise service.ValidationError(f"limit must be non-negative, got {value}")
    return min(value, MAX_LIST_LIMIT)


def _parse_offset(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise service.ValidationError(f"offset must be an integer, got '{raw}'") from None
    if value < 0:
        raise service.ValidationError(f"offset must be non-negative, got {value}")
    return value


def _with_error_handling(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
    @wraps(handler)
    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except Exception as exc:  # translated uniformly via error_payload
            status, body = error_payload(exc)
            if status == HTTPStatus.INTERNAL_SERVER_ERROR:
                # NotFoundError/ConflictError/ValidationError etc. are
                # expected control flow with their own 4xx status — only an
                # exception error_payload couldn't map at all is logged
                # here, since that's the case where the real traceback
                # would otherwise vanish behind a bare {"error":
                # "internal_error"} with nothing in the server logs to
                # debug from.
                logger.exception("Unhandled exception in %s %s", request.method, request.url.path)
            return JSONResponse(body, status_code=status.value)

    return wrapped


# ---------------------------------------------------------------------------
# Programmes
# ---------------------------------------------------------------------------


@mcp.endpoint("/v1/programmes", methods=["GET"])
@_with_error_handling
async def programmes_collection(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    return _ok(await service.list_programmes())


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Search / index
# ---------------------------------------------------------------------------

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
    return _ok(
        await service.get_index(
            limit=_parse_limit(params.get("limit"), MAX_LIST_LIMIT),
            offset=_parse_offset(params.get("offset")),
        )
    )


# ---------------------------------------------------------------------------
# Queue (spec §6a) — the harness's side of the contract
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

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


@mcp.endpoint("/v1/runs/{run_id}/artifacts", methods=["POST"])
@_with_error_handling
async def run_artifacts(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.WRITE)
    run_id = request.path_params["run_id"]
    data = ArtifactCreate.model_validate(await request.json())
    return _ok(await service.register_artifact(run_id, data), status=HTTPStatus.CREATED)


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


# ---------------------------------------------------------------------------
# Artifacts — R2 presign/download (spec §4/§9). Reports itself as not
# implemented if R2 secrets aren't set on this deployment, rather than
# raising — a server without R2 configured should still serve everything
# else normally.
# ---------------------------------------------------------------------------


@mcp.endpoint("/v1/runs/{run_id}/artifacts/presign", methods=["POST"])
@_with_error_handling
async def run_artifacts_presign(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.WRITE)
    if not settings.r2_configured:
        return JSONResponse(_R2_NOT_CONFIGURED, status_code=HTTPStatus.NOT_IMPLEMENTED.value)

    run_id = request.path_params["run_id"]
    await service.get_run(run_id)  # 404s if the run doesn't exist
    data = ArtifactPresignRequest.model_validate(await request.json())
    key = f"runs/{run_id}/{data.kind.value}/{data.filename}"
    upload_url = storage.presign_put(key, content_type=data.content_type)
    return _ok(
        {"upload_url": upload_url, "uri": storage.build_uri(key), "expires_in": PRESIGN_PUT_EXPIRY_SECONDS},
        status=HTTPStatus.CREATED,
    )


def _decode_artifact_content(content_base64: str) -> tuple[bytes | None, str | None]:
    """(content, None) on success, (None, error message) on failure — shared
    by the single-item and batch upload routes so both report bad input
    identically."""
    try:
        content = base64.b64decode(content_base64, validate=True)
    except binascii.Error:
        return None, "content_base64 is not valid base64"
    if len(content) > _MAX_INLINE_BASE64_BYTES:
        return None, (
            f"content exceeds {_MAX_INLINE_BASE64_BYTES} bytes for an inline base64 "
            "upload — use POST .../artifacts/upload-raw (multipart, streamed from "
            "disk, no size penalty on the caller's own context) or the R2 presign "
            "flow instead"
        )
    return content, None


async def _upload_or_dedup_artifact(
    run_id: str,
    *,
    name: str,
    kind: str,
    role: ArtifactRole | None,
    filename: str,
    content: bytes,
    meta: dict[str, Any],
) -> Any:
    """Content-addressed by (name, sha256): if this exact content was
    already uploaded under this name — by an earlier run, or an earlier item
    in the same batch — that upload is reused (role=used) instead of
    uploading again. Shared by every upload route (JSON single/batch,
    multipart raw) so there's exactly one dedup implementation regardless
    of how the bytes arrived."""
    content_sha256 = hashlib.sha256(content).hexdigest()
    existing = await service.find_artifact_by_name_sha(name, content_sha256)
    if existing is not None:
        uri = existing.uri
        # drive_url is pinned to the original upload's value (never the
        # caller's) — a caller-controlled meta.drive_url would otherwise let
        # them redirect a future /download to an arbitrary URL, since
        # artifact_download follows this field unconditionally.
        merged_meta = {**meta, "source_path": filename}
        if "drive_url" in existing.meta:
            merged_meta["drive_url"] = existing.meta["drive_url"]
        resolved_role = role or ArtifactRole.USED
    else:
        drive_client = drive_storage.get_client()
        root_id = drive_storage.ensure_folder(drive_client, drive_storage.ARCHIVE_ROOT_NAME, None)
        parent_id = drive_storage.ensure_folder_path(
            drive_client, root_id, ("artifacts", name, content_sha256[:12])
        )
        file_id = drive_storage.upload_bytes(drive_client, filename, content, parent_id)
        uri = f"{GDRIVE_URI_PREFIX}{file_id}"
        merged_meta = {
            **meta,
            "drive_url": drive_storage.drive_url(file_id),
            "source_path": filename,
        }
        resolved_role = role or ArtifactRole.PRODUCED

    artifact_data = ArtifactCreate(
        kind=kind,
        uri=uri,
        sha256=content_sha256,
        meta=merged_meta,
        name=name,
        role=resolved_role,
    )
    return await service.register_artifact(run_id, artifact_data)


@mcp.endpoint("/v1/runs/{run_id}/artifacts/upload", methods=["POST"])
@_with_error_handling
async def run_artifacts_upload(request: Request) -> Response:
    """Content travels through this server as base64 (unlike the R2 presign
    flow, where bytes never transit it at all) and gets uploaded straight to
    Google Drive — for small provenance/config/log/dataset files an agent
    has bytes for right now, not large checkpoints (those belong in R2).
    Hard cap: content_base64 must decode to at most _MAX_INLINE_BASE64_BYTES
    (32KB) — larger content is rejected with a 400 pointing at upload-raw.

    Content-addressed by (name, sha256): if this exact content was already
    uploaded under this name by an earlier run, that upload is reused
    (role=used) instead of uploading again — a harness reused across many
    runs is only ever stored once. Uploading several files at once? Use
    POST .../artifacts/upload-batch instead of N calls to this route."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    if not settings.google_drive_configured:
        return JSONResponse(_DRIVE_NOT_CONFIGURED, status_code=HTTPStatus.NOT_IMPLEMENTED.value)

    run_id = request.path_params["run_id"]
    await service.get_run(run_id)  # 404s if the run doesn't exist
    data = ArtifactUploadRequest.model_validate(await request.json())

    content, error = _decode_artifact_content(data.content_base64)
    if error is not None:
        return JSONResponse({"error": error}, status_code=HTTPStatus.BAD_REQUEST.value)

    artifact = await _upload_or_dedup_artifact(
        run_id,
        name=data.name,
        kind=data.kind,
        role=data.role,
        filename=data.filename,
        content=content,
        meta=data.meta,
    )
    return _ok(artifact, status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/runs/{run_id}/artifacts/upload-batch", methods=["POST"])
@_with_error_handling
async def run_artifacts_upload_batch(request: Request) -> Response:
    """Same content-addressed upload as .../artifacts/upload, but N files in
    one round trip instead of N separate calls — each item dedups
    independently, including against an earlier item in the same batch (a
    harness file registered twice in one batch is only uploaded once). Same
    32KB-decoded-per-item hard cap as .../artifacts/upload.

    All items are base64-decoded and size-checked before anything is
    uploaded — one bad item fails the whole batch with no partial uploads.
    Returns a JSON array of created artifacts, in request order."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    if not settings.google_drive_configured:
        return JSONResponse(_DRIVE_NOT_CONFIGURED, status_code=HTTPStatus.NOT_IMPLEMENTED.value)

    run_id = request.path_params["run_id"]
    await service.get_run(run_id)  # 404s if the run doesn't exist
    batch = ArtifactBatchUploadRequest.model_validate(await request.json())

    decoded_items: list[bytes] = []
    for index, item in enumerate(batch.items):
        content, error = _decode_artifact_content(item.content_base64)
        if error is not None:
            return JSONResponse(
                {"error": f"items[{index}] ({item.filename}): {error}"},
                status_code=HTTPStatus.BAD_REQUEST.value,
            )
        decoded_items.append(content)

    results = [
        await _upload_or_dedup_artifact(
            run_id,
            name=item.name,
            kind=item.kind,
            role=item.role,
            filename=item.filename,
            content=content,
            meta=item.meta,
        )
        for item, content in zip(batch.items, decoded_items, strict=True)
    ]
    return _ok(results, status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/runs/{run_id}/artifacts/upload-raw", methods=["POST"])
@_with_error_handling
async def run_artifacts_upload_raw(request: Request) -> Response:
    """Multipart upload for a real file from a shell (`curl -F
    file=@path`), never an MCP tool call — the bytes never pass through
    the calling model's own context this way, unlike `.../upload`'s
    `content_base64` (an MCP tool argument, which the calling model must
    emit as literal text and which therefore shows up in full in its own
    transcript). Prefer this route for anything beyond a trivial inline
    size, especially from a remote/sandboxed environment where installing
    this project's own package isn't practical — curl needs nothing
    installed at all.

    Form fields: file (required), name (required, dedup key), kind
    (optional, default "other"), role (optional, auto-inferred if
    omitted), meta (optional, a JSON-encoded object string)."""
    await auth.require_scope_from_request(request, Scope.WRITE)
    if not settings.google_drive_configured:
        return JSONResponse(_DRIVE_NOT_CONFIGURED, status_code=HTTPStatus.NOT_IMPLEMENTED.value)

    run_id = request.path_params["run_id"]
    await service.get_run(run_id)  # 404s if the run doesn't exist

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "file is required"}, status_code=HTTPStatus.BAD_REQUEST.value)
    name = form.get("name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=HTTPStatus.BAD_REQUEST.value)

    content = await upload.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": f"content exceeds {_MAX_UPLOAD_BYTES} bytes — use the R2 presign flow for large files"},
            status_code=HTTPStatus.BAD_REQUEST.value,
        )

    try:
        meta = json.loads(form["meta"]) if form.get("meta") else {}
    except (json.JSONDecodeError, TypeError):
        return JSONResponse(
            {"error": "meta must be a JSON-encoded object"}, status_code=HTTPStatus.BAD_REQUEST.value
        )
    try:
        role = ArtifactRole(form["role"]) if form.get("role") else None
    except ValueError:
        return JSONResponse(
            {"error": f"role must be one of {[r.value for r in ArtifactRole]}"},
            status_code=HTTPStatus.BAD_REQUEST.value,
        )

    artifact = await _upload_or_dedup_artifact(
        run_id,
        name=name,
        kind=form.get("kind", "other"),
        role=role,
        filename=upload.filename or name,
        content=content,
        meta=meta,
    )
    return _ok(artifact, status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/artifacts/{artifact_id:int}/download", methods=["GET"])
@_with_error_handling
async def artifact_download(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    artifact = await service.get_artifact(request.path_params["artifact_id"])

    if artifact.uri.startswith(GDRIVE_URI_PREFIX):
        # Archived by scripts/archive_*_to_drive.py — no presigning needed,
        # the folder link is already there in meta (drive.file scope means
        # only the archiving Google account can view it, which is the same
        # account the dashboard's Google sign-in is restricted to).
        #
        # register_artifact accepts arbitrary caller-supplied meta, so
        # drive_url isn't necessarily trustworthy just because it's present
        # — checked against Drive's real domain before ever being used as a
        # redirect target, so a crafted meta can't turn this into an open
        # redirect.
        drive_url = artifact.meta.get("drive_url")
        if not drive_url or not drive_url.startswith(TRUSTED_DRIVE_URL_PREFIX):
            return JSONResponse(
                {"error": "artifact has no valid drive_url in meta"},
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
            )
        return RedirectResponse(drive_url, status_code=HTTPStatus.FOUND.value)

    if artifact.uri.startswith(GIT_URI_PREFIXES) or artifact.uri.startswith(HF_URI_PREFIX):
        # There's no single file to download for a git commit or an HF Hub
        # revision — the dashboard renders a real github.com/huggingface.co
        # link from meta instead (see app.html); this route just needs to
        # say so clearly rather than falling through to storage.key_from_uri
        # and raising an unhandled ValueError.
        return JSONResponse(
            {"error": "artifact is a git+/hf:// reference, not a single downloadable file — see its meta"},
            status_code=HTTPStatus.BAD_REQUEST.value,
        )

    if not settings.r2_configured:
        return JSONResponse(_R2_NOT_CONFIGURED, status_code=HTTPStatus.NOT_IMPLEMENTED.value)

    download_url = storage.presign_get(storage.key_from_uri(artifact.uri))
    return RedirectResponse(download_url, status_code=HTTPStatus.FOUND.value)


@mcp.endpoint("/v1/artifacts/{artifact_id:int}/lineage", methods=["GET"])
@_with_error_handling
async def artifact_lineage(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    return _ok(await service.get_artifact_lineage(request.path_params["artifact_id"]))


@mcp.endpoint("/v1/artifacts/{artifact_id:int}/verify", methods=["POST"])
@_with_error_handling
async def artifact_verify(request: Request) -> Response:
    """Live-checks a git+/hf:// reference artifact and caches the result —
    POST, not GET, since this makes an outbound network call and writes two
    columns; not a cacheable read like lineage. See external_refs.py for why
    this exists: a name match alone isn't enough (2026-07-19 larql
    near-miss — an HF repo existed but was missing the real weight files)."""
    api_key = await auth.require_scope_from_request(request, Scope.WRITE)
    requesting_user_id = api_key.created_by_user_id if api_key else None
    return _ok(
        await service.verify_artifact(
            request.path_params["artifact_id"], requesting_user_id=requesting_user_id
        )
    )


@mcp.endpoint("/v1/artifacts/external-refs", methods=["GET"])
@_with_error_handling
async def artifacts_external_refs(request: Request) -> Response:
    """Every git+/hf:// reference artifact across all experiments — a
    run-detail page only shows one run's artifacts, this is the dashboard-
    wide "what do we point at outside this server, and is it still there"
    browse view (roadmap item 5, 2026-07-19)."""
    await auth.require_scope_from_request(request, Scope.READ)
    params = request.query_params
    return _ok(
        await service.list_external_ref_artifacts(
            limit=_parse_limit(params.get("limit"), DEFAULT_LIST_LIMIT),
            offset=_parse_offset(params.get("offset")),
        )
    )


# ---------------------------------------------------------------------------
# Pins — named, repointable aliases to a specific artifact (e.g.
# "tok-v12-tokenizer:latest"), W&B-style.
# ---------------------------------------------------------------------------


@mcp.endpoint("/v1/pins", methods=["GET"])
@_with_error_handling
async def pins_collection(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    return _ok(await service.list_pins())


@mcp.endpoint("/v1/pins/{name}", methods=["GET", "PUT"])
@_with_error_handling
async def pin_detail(request: Request) -> Response:
    name = request.path_params["name"]
    if request.method == "GET":
        await auth.require_scope_from_request(request, Scope.READ)
        return _ok(await service.get_pin(name))

    await auth.require_scope_from_request(request, Scope.WRITE)
    data = ArtifactPinSet.model_validate(await request.json())
    return _ok(await service.set_pin(name, data.artifact_id))


# ---------------------------------------------------------------------------
# Dashboard users & self-service API keys (team management)
#
# Gated by auth.require_dashboard_role, not require_scope_from_request —
# minting credentials/adding collaborators is a different, more sensitive
# axis than the Scope-based bearer/cookie auth the rest of this file uses.
# ---------------------------------------------------------------------------


@mcp.endpoint("/v1/me", methods=["GET"])
@_with_error_handling
async def me(request: Request) -> Response:
    identity = await auth.require_dashboard_role(request, Scope.READ)
    token_status = await service.get_user_token_status(identity.user_id)
    return _ok({"email": identity.email, "role": identity.role.value, **token_status})


@mcp.endpoint("/v1/me/tokens/{provider}", methods=["PUT", "DELETE"])
@_with_error_handling
async def me_token_item(request: Request) -> Response:
    """Self-service GitHub/HF token storage for verify_artifact — same
    require_dashboard_role gate as key self-service (any signed-in user
    manages their own, no elevated role needed), never exposed to MCP
    tools (a one-time personal setup action, not something an agent should
    do on a user's behalf)."""
    identity = await auth.require_dashboard_role(request, Scope.READ)
    try:
        provider = TokenProvider(request.path_params["provider"])
    except ValueError:
        return JSONResponse(
            {"error": f"provider must be one of {[p.value for p in TokenProvider]}"},
            status_code=HTTPStatus.BAD_REQUEST.value,
        )
    if request.method == "PUT":
        data = UserTokenSet.model_validate(await request.json())
        await service.set_user_token(identity, provider, data.token)
        return _ok({"provider": provider.value, "set": True})
    await service.clear_user_token(identity, provider)
    return _ok({"provider": provider.value, "set": False})


@mcp.endpoint("/v1/users", methods=["GET", "POST"])
@_with_error_handling
async def users_collection(request: Request) -> Response:
    await auth.require_dashboard_role(request, Scope.ADMIN)
    if request.method == "GET":
        return _ok(await service.list_team_users())
    data = AppUserCreate.model_validate(await request.json())
    return _ok(await service.create_user(data.email, data.role), status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/users/{user_id:int}", methods=["DELETE"])
@_with_error_handling
async def user_item(request: Request) -> Response:
    await auth.require_dashboard_role(request, Scope.ADMIN)
    await service.revoke_user(request.path_params["user_id"])
    return _ok({"revoked": True})


@mcp.endpoint("/v1/keys", methods=["GET", "POST"])
@_with_error_handling
async def keys_collection(request: Request) -> Response:
    identity = await auth.require_dashboard_role(request, Scope.READ)
    if request.method == "GET":
        return _ok(await service.list_api_keys(identity))
    data = ApiKeyCreate.model_validate(await request.json())
    return _ok(await service.create_api_key(identity, data.name, data.scopes), status=HTTPStatus.CREATED)


@mcp.endpoint("/v1/keys/{key_id:int}", methods=["DELETE"])
@_with_error_handling
async def key_item(request: Request) -> Response:
    identity = await auth.require_dashboard_role(request, Scope.READ)
    await service.revoke_api_key(identity, request.path_params["key_id"])
    return _ok({"revoked": True})
