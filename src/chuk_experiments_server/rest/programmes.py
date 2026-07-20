from starlette.requests import Request
from starlette.responses import Response

from .. import auth, service
from ..constants import Scope
from ..server import mcp
from ._shared import _ok, _with_error_handling


@mcp.endpoint("/v1/programmes", methods=["GET"])
@_with_error_handling
async def programmes_collection(request: Request) -> Response:
    await auth.require_scope_from_request(request, Scope.READ)
    return _ok(await service.list_programmes())
