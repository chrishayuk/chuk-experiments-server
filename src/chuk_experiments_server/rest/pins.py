"""Named, repointable aliases to a specific artifact (e.g.
"tok-v12-tokenizer:latest"), W&B-style."""

from starlette.requests import Request
from starlette.responses import Response

from .. import auth, service
from ..constants import Scope
from ..models import ArtifactPinSet
from ..server import mcp
from ._shared import _ok, _with_error_handling


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
