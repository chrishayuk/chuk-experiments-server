"""Dashboard route tests. web.py serves the OAuth flow, the SPA-shell route
(templates/app.html), and the split-out static JS it loads — all data pages
are client-side JS fetching /v1/* directly, already covered by
test_rest.py/test_auth.py, so there's nothing page-content-specific left to
test at the HTTP level beyond that."""

from http import HTTPStatus

from chuk_experiments_server import webauth
from chuk_experiments_server.config import settings
from chuk_experiments_server.constants import OAUTH_STATE_COOKIE_NAME, SESSION_COOKIE_NAME


async def _create_experiment(dashboard_client, write_key, **overrides):
    body = {"programme": "cn", "slug": "cn-7", "title": "t", **overrides}
    return await dashboard_client.post(
        "/v1/experiments", json=body, headers={"Authorization": f"Bearer {write_key}"}
    )


# --- Auth gate ---------------------------------------------------------------


async def test_overview_redirects_when_unauthenticated(dashboard_client):
    resp = await dashboard_client.get("/", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "/login"


async def test_overview_open_access_when_dashboard_auth_not_configured(dashboard_client, monkeypatch):
    """Google sign-in only gates the dashboard once it's actually
    configured (Fly secrets in production) — local dev, with no Google
    credentials set, gets straight in rather than a dead-end redirect to a
    /login page that itself can't be used."""
    monkeypatch.setattr(type(settings), "dashboard_auth_configured", property(lambda self: False))
    resp = await dashboard_client.get("/")
    assert resp.status_code == HTTPStatus.OK
    assert 'id="app"' in resp.text


async def test_overview_renders_shell_when_authenticated(dashboard_client, authenticated_cookies):
    resp = await dashboard_client.get("/", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK
    assert 'id="app"' in resp.text
    assert "chuk-<b>experiments</b>" in resp.text
    assert "chrishayuk@googlemail.com" in resp.text  # signed-in user shown in the header


async def test_overview_shell_omits_user_span_when_dashboard_auth_not_configured(
    dashboard_client, monkeypatch
):
    monkeypatch.setattr(type(settings), "dashboard_auth_configured", property(lambda self: False))
    resp = await dashboard_client.get("/")
    assert "Sign out" not in resp.text


async def test_overview_shell_injects_status_enum_constants(dashboard_client, authenticated_cookies):
    """STATUS_CLASS/EXPERIMENT_STATUSES/ROLE_SCOPE_CEILING used to be
    hand-copied JS literals kept in sync by hand — now server-injected from
    the real Python constants, so prove the actual values land in the
    rendered page rather than an empty/broken template substitution."""
    resp = await dashboard_client.get("/", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK
    assert '"completed": "good"' in resp.text
    assert '"draft", "planned", "running", "completed", "abandoned", "superseded"' in resp.text
    assert '"admin": ["read", "write", "admin"]' in resp.text


async def test_login_page_redirects_if_already_authenticated(dashboard_client, authenticated_cookies):
    resp = await dashboard_client.get("/login", cookies=authenticated_cookies, follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "/"


async def test_login_page_shows_sign_in_button(dashboard_client):
    resp = await dashboard_client.get("/login")
    assert resp.status_code == HTTPStatus.OK
    assert "Sign in with Google" in resp.text


async def test_login_page_unavailable_when_dashboard_auth_not_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(type(settings), "dashboard_auth_configured", property(lambda self: False))
    resp = await dashboard_client.get("/login")
    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE


async def test_logout_clears_session_cookie(dashboard_client, authenticated_cookies):
    resp = await dashboard_client.get("/logout", cookies=authenticated_cookies, follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "/login"
    # httpx's response.cookies jar doesn't surface an immediately-expired
    # cookie, so check the raw Set-Cookie header instead of resp.cookies.
    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    assert "Max-Age=0" in set_cookie or "01 Jan 1970" in set_cookie


# --- OAuth flow ------------------------------------------------------------


async def test_login_google_sets_state_cookie_and_redirects(dashboard_client):
    resp = await dashboard_client.get("/login/google", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert "accounts.google.com" in resp.headers["location"]
    assert OAUTH_STATE_COOKIE_NAME in resp.cookies


async def test_auth_callback_rejects_mismatched_state(dashboard_client):
    resp = await dashboard_client.get(
        "/auth/callback",
        params={"code": "x", "state": "mismatched"},
        cookies={OAUTH_STATE_COOKIE_NAME: "expected"},
        follow_redirects=False,
    )
    assert resp.status_code == HTTPStatus.FOUND
    assert "error" in resp.headers["location"]


async def test_auth_callback_success_sets_session_cookie(dashboard_client, monkeypatch):
    async def fake_exchange(code):
        return "chrishayuk@googlemail.com"

    monkeypatch.setattr(webauth, "exchange_code_for_email", fake_exchange)

    resp = await dashboard_client.get(
        "/auth/callback",
        params={"code": "good-code", "state": "expected"},
        cookies={OAUTH_STATE_COOKIE_NAME: "expected"},
        follow_redirects=False,
    )
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "/"
    assert SESSION_COOKIE_NAME in resp.cookies


async def test_auth_callback_handles_google_auth_error(dashboard_client, monkeypatch):
    async def fake_exchange(code):
        raise webauth.GoogleAuthError("token exchange failed")

    monkeypatch.setattr(webauth, "exchange_code_for_email", fake_exchange)

    resp = await dashboard_client.get(
        "/auth/callback",
        params={"code": "bad-code", "state": "expected"},
        cookies={OAUTH_STATE_COOKIE_NAME: "expected"},
        follow_redirects=False,
    )
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "/login?error=sign-in+failed"


async def test_auth_callback_rejects_non_allowed_email(dashboard_client, monkeypatch):
    async def fake_exchange(code):
        return "someone-else@example.com"

    monkeypatch.setattr(webauth, "exchange_code_for_email", fake_exchange)

    resp = await dashboard_client.get(
        "/auth/callback",
        params={"code": "good-code", "state": "expected"},
        cookies={OAUTH_STATE_COOKIE_NAME: "expected"},
        follow_redirects=False,
    )
    assert resp.status_code == HTTPStatus.FOUND
    assert "not+authorized" in resp.headers["location"]


# --- Static assets -----------------------------------------------------------


async def test_static_asset_serves_known_js_file(dashboard_client):
    """No auth gate on these — they're the same app.html JS every visitor
    already gets inline before the split into static/*.js files."""
    resp = await dashboard_client.get("/static/app-core.js")
    assert resp.status_code == HTTPStatus.OK
    assert resp.headers["content-type"].startswith("application/javascript")
    assert "function pagerHtml" in resp.text


async def test_static_asset_unknown_filename_404s(dashboard_client):
    """The allowlist built at import time (see web.py's _STATIC_JS) is the
    whole path-traversal defense — an unrecognized filename never reaches a
    filesystem lookup at all, it just isn't a dict key."""
    resp = await dashboard_client.get("/static/../../etc/passwd")
    assert resp.status_code == HTTPStatus.NOT_FOUND

    resp = await dashboard_client.get("/static/nonexistent.js")
    assert resp.status_code == HTTPStatus.NOT_FOUND
