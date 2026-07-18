"""Dashboard (spec §8, Phase 4) — read-only, server-rendered. Every page
calls this server's own REST API (see internal_client.py) using a fixed
internal service key, never service.py directly — the human's identity is
already verified by webauth.py's Google sign-in gate; the internal key just
satisfies the REST layer's bearer-auth requirement on their behalf.
"""

from functools import wraps
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from . import internal_client, webauth
from .config import settings
from .constants import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    ExperimentStatus,
    OAUTH_STATE_COOKIE_MAX_AGE_SECONDS,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
)
from .markdown_render import render as render_markdown
from .server import mcp

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _format_datetime(value: str | None) -> str:
    """REST responses are JSON, so datetime fields arrive as ISO 8601
    strings (e.g. "2026-07-18T14:44:15.750784+00:00"), not datetime objects
    — no .strftime() available in templates, hence this filter."""
    if not value:
        return "—"
    return value[:16].replace("T", " ")


_templates.env.filters["fmt_dt"] = _format_datetime


class DashboardAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def _api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    client = internal_client.get_client()
    resp = await client.get(
        path, params=params, headers={"Authorization": f"Bearer {settings.internal_api_key}"}
    )
    if resp.status_code >= 400:
        raise DashboardAPIError(resp.status_code, resp.json().get("error", "request failed"))
    return resp.json()


def _render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    context["user_email"] = webauth.get_authenticated_email(request)
    return _templates.TemplateResponse(request, name, context)


def _dashboard_route(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
    @wraps(handler)
    async def wrapped(request: Request) -> Response:
        if not webauth.is_authenticated(request):
            return RedirectResponse("/login", status_code=HTTPStatus.FOUND.value)
        try:
            return await handler(request)
        except DashboardAPIError as exc:
            status = HTTPStatus.NOT_FOUND if exc.status_code == 404 else HTTPStatus.BAD_GATEWAY
            return HTMLResponse(f"<h1>{status.value}</h1><p>{exc.message}</p>", status_code=status.value)

    return wrapped


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@mcp.endpoint("/login", methods=["GET"])
async def login_page(request: Request) -> Response:
    if webauth.is_authenticated(request):
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

    if email != settings.dashboard_allowed_email:
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
# Pages
# ---------------------------------------------------------------------------


@mcp.endpoint("/", methods=["GET"])
@_dashboard_route
async def overview(request: Request) -> Response:
    programmes = await _api_get("/v1/programmes")
    recent = await _api_get("/v1/experiments", params={"limit": 10})
    running = await _api_get(
        "/v1/experiments", params={"status": ExperimentStatus.RUNNING.value, "limit": 10}
    )
    return _render(request, "index.html", {"programmes": programmes, "recent": recent, "running": running})


@mcp.endpoint("/experiments", methods=["GET"])
@_dashboard_route
async def experiments_list(request: Request) -> Response:
    params = request.query_params
    programme = params.get("programme") or None
    status = params.get("status") or None
    tag = params.get("tag") or None
    limit = int(params.get("limit", DEFAULT_LIST_LIMIT))

    api_params: dict[str, Any] = {"limit": limit + 1}
    if programme:
        api_params["programme"] = programme
    if status:
        api_params["status"] = status
    if tag:
        api_params["tag"] = tag

    experiments = await _api_get("/v1/experiments", params=api_params)
    has_more = len(experiments) > limit
    experiments = experiments[:limit]

    next_page_params = {k: v for k, v in {"programme": programme, "status": status, "tag": tag}.items() if v}
    next_page_params["limit"] = limit + DEFAULT_LIST_LIMIT

    return _render(
        request,
        "experiments.html",
        {
            "experiments": experiments,
            "programmes": await _api_get("/v1/programmes"),
            "statuses": [s.value for s in ExperimentStatus],
            "filters": {"programme": programme, "status": status, "tag": tag},
            "has_more": has_more,
            "next_page_url": "/experiments?" + "&".join(f"{k}={v}" for k, v in next_page_params.items()),
        },
    )


@mcp.endpoint("/experiments/{slug}", methods=["GET"])
@_dashboard_route
async def experiment_detail(request: Request) -> Response:
    slug = request.path_params["slug"]
    experiment = await _api_get(f"/v1/experiments/{slug}")
    writeup_html = (
        render_markdown(experiment["latest_writeup"]["body_md"]) if experiment.get("latest_writeup") else ""
    )
    return _render(
        request, "experiment_detail.html", {"experiment": experiment, "writeup_html": writeup_html}
    )


@mcp.endpoint("/runs/{run_id}", methods=["GET"])
@_dashboard_route
async def run_detail(request: Request) -> Response:
    run = await _api_get(f"/v1/runs/{request.path_params['run_id']}")
    return _render(request, "run_detail.html", {"run": run})


@mcp.endpoint("/search", methods=["GET"])
@_dashboard_route
async def search_page(request: Request) -> Response:
    query = request.query_params.get("q") or None
    hits = await _api_get("/v1/search", params={"q": query, "limit": DEFAULT_SEARCH_LIMIT}) if query else []
    return _render(request, "search.html", {"query": query, "hits": hits})


@mcp.endpoint("/artifacts/{artifact_id:int}/download", methods=["GET"])
@_dashboard_route
async def artifact_download_redirect(request: Request) -> Response:
    """Proxies the REST API's own presign redirect: a browser can't attach
    the internal bearer token itself, so this server makes that call and
    forwards the resulting R2 URL as its own redirect instead."""
    client = internal_client.get_client()
    resp = await client.get(
        f"/v1/artifacts/{request.path_params['artifact_id']}/download",
        headers={"Authorization": f"Bearer {settings.internal_api_key}"},
        follow_redirects=False,
    )
    if resp.status_code == HTTPStatus.FOUND:
        return RedirectResponse(resp.headers["location"], status_code=HTTPStatus.FOUND.value)
    if resp.status_code >= 400:
        raise DashboardAPIError(resp.status_code, resp.json().get("error", "download failed"))
    return HTMLResponse("<h1>Unexpected response</h1>", status_code=HTTPStatus.BAD_GATEWAY.value)
