"""webauth.py tests — sign/verify and the OAuth code-exchange logic are pure
enough to unit-test directly; get_authenticated_email/is_authenticated take
a duck-typed fake request (just needs a `.cookies` dict) rather than a full
ASGI Request, since that's all they read."""

import time

import pytest

from chuk_experiments_server import webauth
from chuk_experiments_server.config import settings
from chuk_experiments_server.constants import SESSION_COOKIE_NAME


class _FakeRequest:
    def __init__(self, cookies: dict):
        self.cookies = cookies


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, token_response, userinfo_response):
        self._token_response = token_response
        self._userinfo_response = userinfo_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, data=None):
        return self._token_response

    async def get(self, url, headers=None):
        return self._userinfo_response


def test_sign_verify_roundtrip():
    token = webauth.create_session_cookie_value("chris@example.com")
    assert webauth._verify(token) == "chris@example.com"


def test_verify_rejects_tampered_payload():
    token = webauth.create_session_cookie_value("chris@example.com")
    payload, _, signature = token.rpartition(".")
    tampered = payload.replace("chris", "attacker") + "." + signature
    assert webauth._verify(tampered) is None


def test_verify_rejects_missing_signature():
    assert webauth._verify("no-dot-here") is None


def test_verify_rejects_expired_token():
    expired_payload = f"chris@example.com:{int(time.time()) - 10}"
    expired_token = webauth._sign(expired_payload)
    assert webauth._verify(expired_token) is None


def test_get_authenticated_email_no_cookie():
    assert webauth.get_authenticated_email(_FakeRequest({})) is None


def test_get_authenticated_email_valid_cookie():
    token = webauth.create_session_cookie_value("chris@example.com")
    request = _FakeRequest({SESSION_COOKIE_NAME: token})
    assert webauth.get_authenticated_email(request) == "chris@example.com"


def test_get_authenticated_email_missing_session_secret_returns_none(monkeypatch):
    """SESSION_SECRET not being configured at all (e.g. local dev, with
    dashboard auth not set up) must not crash page rendering — any cookie
    present is necessarily stale/foreign, since this server could never
    have signed one without a secret to sign it with."""
    token = webauth.create_session_cookie_value("chris@example.com")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    request = _FakeRequest({SESSION_COOKIE_NAME: token})
    assert webauth.get_authenticated_email(request) is None


def test_is_authenticated_matches_allowed_email():
    token = webauth.create_session_cookie_value(settings.dashboard_allowed_email)
    request = _FakeRequest({SESSION_COOKIE_NAME: token})
    assert webauth.is_authenticated(request)


def test_is_authenticated_rejects_other_email():
    token = webauth.create_session_cookie_value("someone-else@example.com")
    request = _FakeRequest({SESSION_COOKIE_NAME: token})
    assert not webauth.is_authenticated(request)


def test_new_oauth_state_is_unique():
    assert webauth.new_oauth_state() != webauth.new_oauth_state()


def test_build_google_auth_url_includes_client_and_state():
    url = webauth.build_google_auth_url("some-state")
    assert "accounts.google.com" in url
    assert "state=some-state" in url
    assert settings.google_client_id in url


async def test_exchange_code_for_email_success(monkeypatch):
    token_resp = _FakeResponse(200, {"access_token": "fake-token"})
    userinfo_resp = _FakeResponse(200, {"email": "chris@example.com", "email_verified": True})
    monkeypatch.setattr(
        webauth.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(token_resp, userinfo_resp)
    )

    email = await webauth.exchange_code_for_email("some-code")
    assert email == "chris@example.com"


async def test_exchange_code_for_email_token_failure(monkeypatch):
    token_resp = _FakeResponse(400, {"error": "invalid_grant"})
    monkeypatch.setattr(webauth.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(token_resp, None))

    with pytest.raises(webauth.GoogleAuthError):
        await webauth.exchange_code_for_email("bad-code")


async def test_exchange_code_for_email_unverified_email(monkeypatch):
    token_resp = _FakeResponse(200, {"access_token": "fake-token"})
    userinfo_resp = _FakeResponse(200, {"email": "chris@example.com", "email_verified": False})
    monkeypatch.setattr(
        webauth.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(token_resp, userinfo_resp)
    )

    with pytest.raises(webauth.GoogleAuthError, match="not verified"):
        await webauth.exchange_code_for_email("some-code")


async def test_exchange_code_for_email_missing_access_token(monkeypatch):
    token_resp = _FakeResponse(200, {})
    monkeypatch.setattr(webauth.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(token_resp, None))

    with pytest.raises(webauth.GoogleAuthError, match="no access_token"):
        await webauth.exchange_code_for_email("some-code")
