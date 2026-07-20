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
from .constants import AUTHORIZATION_HEADER, BEARER_PREFIX, ROLE_ORDER, Scope
from .db import get_pool
from .models import ApiKey, DashboardIdentity

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
        SELECT id, name, scopes, created_by_user_id FROM api_key
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


async def _active_dashboard_email(request: Request) -> str | None:
    """Cookie-authenticated AND still an active (non-revoked) app_user — a
    revoked user's still-unexpired session cookie must stop granting access
    the moment they're revoked, not wait up to 7 days for the cookie to
    expire on its own. Does its own minimal query rather than calling into
    service/'s richer get_active_user_by_email: service/ already
    imports this module (for generate_key/hash_key), so the reverse import
    would be circular — this one raw query is the cheaper side to duplicate."""
    email = webauth.get_authenticated_email(request)
    if not email:
        return None
    pool = await get_pool()
    row = await pool.fetchrow("SELECT 1 FROM app_user WHERE email = $1 AND revoked_at IS NULL", email)
    return email if row else None


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
    if scope == Scope.READ and not settings.dashboard_auth_configured:
        return None
    if scope == Scope.READ and await _active_dashboard_email(request) is not None:
        return None
    raise AuthError("Missing or invalid API key", status_code=HTTPStatus.UNAUTHORIZED)


async def require_scope_from_tool(scope: Scope) -> ApiKey:
    return await _require_scope(bearer_from_mcp_context(), scope)


async def require_dashboard_role(request: Request, min_role: Scope) -> DashboardIdentity:
    """Identity/authorization for the user & API-key self-service
    management routes — a separate axis from the Scope-based bearer/cookie
    auth above, since minting credentials is more sensitive than reading
    experiment data. Deliberately has NO "dashboard auth unconfigured = free
    pass" fallback (unlike require_scope_from_request's READ path): these
    routes must always be backed by a real ADMIN bearer token or a real
    signed-in, sufficiently-privileged user — that's what makes the feature
    safe to ship even before Google auth is configured anywhere."""
    token = bearer_from_request(request)
    if token:
        record = await authenticate(token)
        if record is not None and record.has_scope(Scope.ADMIN):
            # No specific human behind this key (e.g. dev-local-key, or any
            # CLI-created key) — mirrors created_by_user_id=NULL elsewhere.
            return DashboardIdentity(email=None, role=Scope.ADMIN, user_id=None)

    email = webauth.get_authenticated_email(request)
    if not email:
        raise AuthError("Sign in required", status_code=HTTPStatus.UNAUTHORIZED)

    pool = await get_pool()
    row = await pool.fetchrow("SELECT id, role FROM app_user WHERE email = $1 AND revoked_at IS NULL", email)
    if row is None:
        raise AuthError("Sign in required", status_code=HTTPStatus.UNAUTHORIZED)

    role = Scope(row["role"])
    if ROLE_ORDER[role] < ROLE_ORDER[min_role]:
        raise AuthError(f"role '{role.value}' cannot access this", status_code=HTTPStatus.FORBIDDEN)
    return DashboardIdentity(email=email, role=role, user_id=row["id"])


async def upsert_bootstrap_key(spec: str) -> None:
    """spec = 'name:scope1|scope2:rawkey' — used to seed a dev/admin key on migrate."""
    name, scope_str, raw = spec.split(":", 2)
    scopes = [Scope(s) for s in scope_str.split("|")]
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO api_key (key_hash, name, scopes, team_id)
        VALUES ($1, $2, $3, (SELECT id FROM team WHERE slug = 'default'))
        ON CONFLICT (key_hash) DO UPDATE SET name = EXCLUDED.name, scopes = EXCLUDED.scopes, revoked_at = NULL
        """,
        hash_key(raw),
        name,
        [s.value for s in scopes],
    )


async def upsert_bootstrap_user(email: str, role: Scope) -> None:
    """Seeds (or reaffirms) the dashboard's first admin on `migrate`, from
    DASHBOARD_ALLOWED_EMAIL — mirrors upsert_bootstrap_key. DO NOTHING on
    conflict: won't clobber a role change made later via the admin screen."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO app_user (team_id, email, role)
        VALUES ((SELECT id FROM team WHERE slug = 'default'), $1, $2)
        ON CONFLICT (email) DO NOTHING
        """,
        email,
        role.value,
    )
