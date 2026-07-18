"""Dashboard (spec §8, Phase 4) — a client-side single-page app, matching
gpu-training-harness's chuk-train dashboard's pattern: one shell page (this
module serves it), everything else is vanilla JS doing fetch() straight
against this server's own REST API (see templates/app.html). No
server-side proxy layer — auth.require_scope_from_request accepts the
dashboard's own Google session cookie as an alternative to a bearer token
for READ-scoped requests, so the browser can call /v1/* directly.
"""

from http import HTTPStatus
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from . import service, webauth
from .config import settings
from .constants import (
    OAUTH_STATE_COOKIE_MAX_AGE_SECONDS,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
)
from .server import mcp

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


async def _active_dashboard_email(request: Request) -> str | None:
    """Cookie-authenticated AND still an active (non-revoked) app_user — a
    revoked collaborator's still-unexpired session cookie shouldn't keep
    working just because it hasn't expired yet."""
    email = webauth.get_authenticated_email(request)
    if not email:
        return None
    return email if await service.get_active_user_by_email(email) is not None else None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@mcp.endpoint("/login", methods=["GET"])
async def login_page(request: Request) -> Response:
    if await _active_dashboard_email(request) is not None:
        return RedirectResponse("/", status_code=HTTPStatus.FOUND.value)
    if not settings.dashboard_auth_configured:
        return HTMLResponse(
            "<h1>Dashboard sign-in is not configured on this deployment.</h1>", status_code=503
        )
    return _templates.TemplateResponse(request, "login.html", {"error": request.query_params.get("error")})


@mcp.endpoint("/login/google", methods=["GET"])
async def login_google(request: Request) -> Response:
    state = webauth.new_oauth_state()
    response = RedirectResponse(webauth.build_google_auth_url(state), status_code=HTTPStatus.FOUND.value)
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        state,
        max_age=OAUTH_STATE_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@mcp.endpoint("/auth/callback", methods=["GET"])
async def auth_callback(request: Request) -> Response:
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    state = request.query_params.get("state")
    code = request.query_params.get("code")
    if not code or not state or not expected_state or state != expected_state:
        return RedirectResponse("/login?error=invalid+sign-in+state", status_code=HTTPStatus.FOUND.value)

    try:
        email = await webauth.exchange_code_for_email(code)
    except webauth.GoogleAuthError:
        return RedirectResponse("/login?error=sign-in+failed", status_code=HTTPStatus.FOUND.value)

    if await service.get_active_user_by_email(email) is None:
        return RedirectResponse("/login?error=not+authorized", status_code=HTTPStatus.FOUND.value)

    response = RedirectResponse("/", status_code=HTTPStatus.FOUND.value)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        webauth.create_session_cookie_value(email),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(OAUTH_STATE_COOKIE_NAME)
    return response


@mcp.endpoint("/logout", methods=["GET"])
async def logout(request: Request) -> Response:
    response = RedirectResponse("/login", status_code=HTTPStatus.FOUND.value)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# SPA shell
# ---------------------------------------------------------------------------


@mcp.endpoint("/", methods=["GET"])
async def app_shell(request: Request) -> Response:
    """Serves the single-page app shell — everything past this is client-side
    JS hash-routing + fetch() against /v1/*. Google sign-in only gates this
    once actually configured (Fly secrets in production); local dev, with
    no Google credentials set, gets straight in."""
    email = await _active_dashboard_email(request)
    if settings.dashboard_auth_configured and email is None:
        return RedirectResponse("/login", status_code=HTTPStatus.FOUND.value)
    return _templates.TemplateResponse(request, "app.html", {"user_email": email})
