"""R2 presign/download (spec §4/§9). Reports itself as not implemented if R2
secrets aren't set on this deployment, rather than raising — a server
without R2 configured should still serve everything else normally."""

import base64
import binascii
import hashlib
import json
from http import HTTPStatus
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .. import auth, drive_storage, service, storage
from ..config import settings
from ..constants import (
    DEFAULT_LIST_LIMIT,
    GDRIVE_URI_PREFIX,
    GIT_URI_PREFIXES,
    HF_URI_PREFIX,
    MAX_INLINE_BASE64_BYTES,
    PRESIGN_PUT_EXPIRY_SECONDS,
    TRUSTED_DRIVE_URL_PREFIX,
    ArtifactRole,
    Scope,
)
from ..models import (
    ArtifactBatchUploadRequest,
    ArtifactCreate,
    ArtifactPresignRequest,
    ArtifactUploadRequest,
)
from ..server import mcp
from ._shared import _ok, _parse_limit, _parse_offset, _with_error_handling

_R2_NOT_CONFIGURED = {"error": "not_implemented", "detail": "R2 is not configured on this server"}
_DRIVE_NOT_CONFIGURED = {
    "error": "not_implemented",
    "detail": "Google Drive is not configured on this server",
}
#: Small provenance/config/log/dataset files only — content travels through
#: this server as base64 in the request body, unlike R2's presign flow
#: (bytes never transit the server at all). Large files belong in R2.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


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
    if len(content) > MAX_INLINE_BASE64_BYTES:
        return None, (
            f"content exceeds {MAX_INLINE_BASE64_BYTES} bytes for an inline base64 "
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
    return await service.register_artifact(artifact_data, run_id=run_id)


@mcp.endpoint("/v1/runs/{run_id}/artifacts/upload", methods=["POST"])
@_with_error_handling
async def run_artifacts_upload(request: Request) -> Response:
    """Content travels through this server as base64 (unlike the R2 presign
    flow, where bytes never transit it at all) and gets uploaded straight to
    Google Drive — for small provenance/config/log/dataset files an agent
    has bytes for right now, not large checkpoints (those belong in R2).
    Hard cap: content_base64 must decode to at most MAX_INLINE_BASE64_BYTES
    (constants.py) — larger content is rejected with a 400 pointing at
    upload-raw.

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
    per-item MAX_INLINE_BASE64_BYTES hard cap as .../artifacts/upload.

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


async def _parse_upload_raw_form(form: Any) -> dict[str, Any] | Response:
    """Validate/parse upload-raw's 5 independent form fields (file, name,
    size, meta, role), returning either the parsed kwargs for
    _upload_or_dedup_artifact or an already-built 400 response — never
    raises: a service.ValidationError would map to 422 (errors.py), and
    this route has always used 400 for form-input problems specifically."""
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

    return {
        "name": name,
        "kind": form.get("kind", "other"),
        "role": role,
        "filename": upload.filename or name,
        "content": content,
        "meta": meta,
    }


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

    parsed = await _parse_upload_raw_form(await request.form())
    if isinstance(parsed, Response):
        return parsed

    artifact = await _upload_or_dedup_artifact(run_id, **parsed)
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
