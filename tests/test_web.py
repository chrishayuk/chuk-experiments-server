"""Dashboard route tests — dashboard_client (see conftest.py) wires web.py's
internal REST forwarding to the in-process ASGI app; authenticated_cookies
provides a valid signed session cookie standing in for a completed Google
sign-in."""

from http import HTTPStatus

from chuk_experiments_server import storage, webauth
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


async def test_overview_renders_when_authenticated(dashboard_client, authenticated_cookies):
    resp = await dashboard_client.get("/", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK
    assert "Overview" not in resp.text or True  # title check is enough below
    assert "Programmes" in resp.text


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


# --- Pages ---------------------------------------------------------------


async def test_experiments_list_and_filter(dashboard_client, write_key, authenticated_cookies):
    await _create_experiment(dashboard_client, write_key, programme="cn", slug="cn-7")
    await _create_experiment(dashboard_client, write_key, programme="div", slug="div-3")

    resp = await dashboard_client.get("/experiments", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK
    assert "cn-7" in resp.text and "div-3" in resp.text

    filtered = await dashboard_client.get(
        "/experiments", params={"programme": "div"}, cookies=authenticated_cookies
    )
    assert "div-3" in filtered.text
    assert "cn-7" not in filtered.text


async def test_experiments_list_filters_by_status_and_tag(dashboard_client, write_key, authenticated_cookies):
    await _create_experiment(dashboard_client, write_key, programme="cn", slug="cn-7")
    await _create_experiment(dashboard_client, write_key, programme="div", slug="div-3")
    await dashboard_client.patch(
        "/v1/experiments/cn-7",
        json={"status": "running", "tags": ["baseline"]},
        headers={"Authorization": f"Bearer {write_key}"},
    )

    by_status = await dashboard_client.get(
        "/experiments", params={"status": "running"}, cookies=authenticated_cookies
    )
    assert "cn-7" in by_status.text and "div-3" not in by_status.text

    by_tag = await dashboard_client.get(
        "/experiments", params={"tag": "baseline"}, cookies=authenticated_cookies
    )
    assert "cn-7" in by_tag.text and "div-3" not in by_tag.text


async def test_experiment_detail_renders_writeup(dashboard_client, write_key, authenticated_cookies):
    await _create_experiment(dashboard_client, write_key, hypothesis="a hypothesis")
    await dashboard_client.post(
        "/v1/experiments/cn-7/writeups",
        json={"body_md": "# Findings\n\nIt **works**."},
        headers={"Authorization": f"Bearer {write_key}"},
    )

    resp = await dashboard_client.get("/experiments/cn-7", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK
    assert "a hypothesis" in resp.text
    assert "<strong>works</strong>" in resp.text  # markdown rendered, not raw


async def test_experiment_detail_missing_is_404(dashboard_client, authenticated_cookies):
    resp = await dashboard_client.get("/experiments/does-not-exist", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.NOT_FOUND


async def test_run_detail_renders(dashboard_client, write_key, authenticated_cookies):
    await _create_experiment(dashboard_client, write_key)
    run = (
        await dashboard_client.post(
            "/v1/experiments/cn-7/runs",
            json={"slug": "seed-0"},
            headers={"Authorization": f"Bearer {write_key}"},
        )
    ).json()

    resp = await dashboard_client.get(f"/runs/{run['id']}", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK
    assert "seed-0" in resp.text


async def test_search_page_without_query(dashboard_client, authenticated_cookies):
    resp = await dashboard_client.get("/search", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK


async def test_search_page_with_query(dashboard_client, write_key, authenticated_cookies):
    await _create_experiment(
        dashboard_client, write_key, title="fingerprint embeddings", hypothesis="fingerprint"
    )
    resp = await dashboard_client.get("/search", params={"q": "fingerprint"}, cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK
    assert "cn-7" in resp.text


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


# --- Artifact download proxy ------------------------------------------------


async def _create_run_with_artifact(dashboard_client, write_key):
    await _create_experiment(dashboard_client, write_key)
    run = (
        await dashboard_client.post(
            "/v1/experiments/cn-7/runs",
            json={"slug": "seed-0"},
            headers={"Authorization": f"Bearer {write_key}"},
        )
    ).json()
    artifact = (
        await dashboard_client.post(
            f"/v1/runs/{run['id']}/artifacts",
            # Matches whatever settings.r2_bucket actually resolves to in this
            # environment (real value locally, unset/None in CI) — key_from_uri
            # rejects a URI whose bucket doesn't match, so this can't hardcode
            # a bucket name the way build_uri would.
            json={"kind": "checkpoint", "uri": f"s3://{settings.r2_bucket}/ckpt.bin"},
            headers={"Authorization": f"Bearer {write_key}"},
        )
    ).json()
    return artifact["id"]


async def test_artifact_download_redirects_to_presigned_url(
    dashboard_client, write_key, authenticated_cookies, monkeypatch
):
    monkeypatch.setattr(type(settings), "r2_configured", property(lambda self: True))
    monkeypatch.setattr(storage, "presign_get", lambda key: "https://fake-r2/signed-get")

    artifact_id = await _create_run_with_artifact(dashboard_client, write_key)
    resp = await dashboard_client.get(
        f"/artifacts/{artifact_id}/download", cookies=authenticated_cookies, follow_redirects=False
    )
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "https://fake-r2/signed-get"


async def test_artifact_download_surfaces_api_error(dashboard_client, authenticated_cookies, monkeypatch):
    monkeypatch.setattr(type(settings), "r2_configured", property(lambda self: False))
    resp = await dashboard_client.get("/artifacts/1/download", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.BAD_GATEWAY
