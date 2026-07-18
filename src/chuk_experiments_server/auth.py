"""API key auth. Keys are bearer tokens; only their sha256 hash is stored.

Works from two call sites:
  - Starlette `Request` (REST endpoints registered via @mcp.endpoint)
  - MCP tool functions, which read the ambient ASGI scope via chuk_mcp_server's
    context module (ChukMCPServer doesn't pass Request into @mcp.tool functions).
"""

import hashlib
import secrets
from http import HTTPStatus

from starlette.requests import Request

from . import webauth
from .config import settings
from .constants import AUTHORIZATION_HEADER, BEARER_PREFIX, Scope
from .db import get_pool
from .models import ApiKey

_KEY_BYTES = 32


class AuthError(Exception):
    def __init__(self, message: str, status_code: HTTPStatus = HTTPStatus.UNAUTHORIZED):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return secrets.token_urlsafe(_KEY_BYTES)


def _bearer_from_header_value(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != BEARER_PREFIX:
        return None
    return token.strip() or None


def bearer_from_request(request: Request) -> str | None:
    return _bearer_from_header_value(request.headers.get(AUTHORIZATION_HEADER))


def bearer_from_mcp_context() -> str | None:
    """Read Authorization from the ambient ASGI scope during an @mcp.tool call."""
    from chuk_mcp_server.context import get_http_request

    scope = get_http_request()
    if not scope:
        return None
    for key, value in scope.get("headers", []):
        if key.decode("latin-1").lower() == AUTHORIZATION_HEADER:
            return _bearer_from_header_value(value.decode("latin-1"))
    return None


async def authenticate(raw_token: str | None) -> ApiKey | None:
    if not raw_token:
        return None
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, name, scopes FROM api_key
        WHERE key_hash = $1 AND revoked_at IS NULL
        """,
        hash_key(raw_token),
    )
    if row is None:
        return None
    return ApiKey.model_validate(dict(row))


async def _require_scope(raw_token: str | None, scope: Scope) -> ApiKey:
    record = await authenticate(raw_token)
    if record is None:
        raise AuthError("Missing or invalid API key", status_code=HTTPStatus.UNAUTHORIZED)
    if not record.has_scope(scope):
        raise AuthError(
            f"API key '{record.name}' lacks '{scope.value}' scope", status_code=HTTPStatus.FORBIDDEN
        )
    return record


async def require_scope_from_request(request: Request, scope: Scope) -> ApiKey | None:
    """A bearer token always works, for any scope. For Scope.READ only, the
    dashboard's own Google session cookie is also accepted (letting the SPA
    call /v1/* directly instead of through a server-side proxy), and — when
    dashboard auth isn't configured at all (local dev) — no credential is
    required, matching local dev's existing open-access behavior. Neither
    fallback ever satisfies WRITE/ADMIN: the dashboard is read-only by
    design, so a browser session should never be able to mutate data."""
    token = bearer_from_request(request)
    if token:
        return await _require_scope(token, scope)
    if scope == Scope.READ and (not settings.dashboard_auth_configured or webauth.is_authenticated(request)):
        return None
    raise AuthError("Missing or invalid API key", status_code=HTTPStatus.UNAUTHORIZED)


async def require_scope_from_tool(scope: Scope) -> ApiKey:
    return await _require_scope(bearer_from_mcp_context(), scope)


async def upsert_bootstrap_key(spec: str) -> None:
    """spec = 'name:scope1|scope2:rawkey' — used to seed a dev/admin key on migrate."""
    name, scope_str, raw = spec.split(":", 2)
    scopes = [Scope(s) for s in scope_str.split("|")]
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO api_key (key_hash, name, scopes)
        VALUES ($1, $2, $3)
        ON CONFLICT (key_hash) DO UPDATE SET name = EXCLUDED.name, scopes = EXCLUDED.scopes, revoked_at = NULL
        """,
        hash_key(raw),
        name,
        [s.value for s in scopes],
    )
