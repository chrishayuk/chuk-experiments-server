"""Dashboard users & self-service API keys (team management).

Gated by auth.require_dashboard_role, not require_scope_from_request —
minting credentials/adding collaborators is a different, more sensitive
axis than the Scope-based bearer/cookie auth the rest of this package uses.
"""

from http import HTTPStatus

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import auth, service
from ..constants import Scope, TokenProvider
from ..models import ApiKeyCreate, AppUserCreate, UserTokenSet
from ..server import mcp
from ._shared import _ok, _with_error_handling


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
