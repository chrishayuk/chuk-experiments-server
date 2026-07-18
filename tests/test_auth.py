from http import HTTPStatus

import pytest

from chuk_experiments_server import auth
from chuk_experiments_server.constants import Scope
from chuk_experiments_server.models import ApiKey


def test_hash_key_is_deterministic_and_not_reversible():
    raw = "some-secret-key"
    assert auth.hash_key(raw) == auth.hash_key(raw)
    assert auth.hash_key(raw) != raw


def test_generate_key_produces_unique_values():
    assert auth.generate_key() != auth.generate_key()


@pytest.mark.parametrize(
    ("header_value", "expected"),
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),  # scheme is case-insensitive
        ("Bearer  ", None),  # empty token
        ("Basic abc123", None),  # wrong scheme
        (None, None),
        ("", None),
    ],
)
def test_bearer_from_header_value(header_value, expected):
    assert auth._bearer_from_header_value(header_value) == expected


def test_api_key_has_scope_direct_match():
    key = ApiKey(id=1, name="test", scopes=[Scope.READ])
    assert key.has_scope(Scope.READ)
    assert not key.has_scope(Scope.WRITE)


def test_api_key_admin_implies_every_scope():
    key = ApiKey(id=1, name="test", scopes=[Scope.ADMIN])
    assert key.has_scope(Scope.READ)
    assert key.has_scope(Scope.WRITE)
    assert key.has_scope(Scope.ADMIN)


async def test_authenticate_unknown_key_returns_none():
    assert await auth.authenticate("not-a-real-key") is None


async def test_authenticate_missing_token_returns_none():
    assert await auth.authenticate(None) is None


async def test_upsert_bootstrap_key_then_authenticate(write_key):
    record = await auth.authenticate(write_key)
    assert record is not None
    assert record.name == "pytest"
    assert record.has_scope(Scope.WRITE)


async def test_revoked_key_does_not_authenticate(write_key):
    from chuk_experiments_server.db import get_pool

    pool = await get_pool()
    await pool.execute("UPDATE api_key SET revoked_at = now() WHERE key_hash = $1", auth.hash_key(write_key))
    assert await auth.authenticate(write_key) is None


def test_bearer_from_mcp_context_reads_ambient_scope():
    from chuk_mcp_server.context import clear_all, set_http_request

    scope = {"headers": [(b"authorization", b"Bearer from-scope-token")]}
    set_http_request(scope)
    try:
        assert auth.bearer_from_mcp_context() == "from-scope-token"
    finally:
        clear_all()


def test_bearer_from_mcp_context_no_authorization_header():
    from chuk_mcp_server.context import clear_all, set_http_request

    set_http_request({"headers": [(b"x-other-header", b"value")]})
    try:
        assert auth.bearer_from_mcp_context() is None
    finally:
        clear_all()


async def test_require_scope_from_tool_raises_on_missing_key():
    # No ambient HTTP request context during a plain pytest run, so
    # bearer_from_mcp_context() naturally returns None here — no mocking needed.
    with pytest.raises(auth.AuthError) as exc_info:
        await auth.require_scope_from_tool(Scope.READ)
    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED


async def test_require_scope_unknown_key_is_unauthorized():
    with pytest.raises(auth.AuthError) as exc_info:
        await auth._require_scope("not-a-real-key", Scope.WRITE)
    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED


async def test_require_scope_insufficient_scope_is_forbidden():
    await auth.upsert_bootstrap_key("readonly:read:readonly-raw-key")
    with pytest.raises(auth.AuthError) as exc_info:
        await auth._require_scope("readonly-raw-key", Scope.WRITE)
    assert exc_info.value.status_code == HTTPStatus.FORBIDDEN


async def test_require_scope_sufficient_scope_returns_key():
    await auth.upsert_bootstrap_key("writer:read|write:writer-raw-key")
    record = await auth._require_scope("writer-raw-key", Scope.WRITE)
    assert record.name == "writer"


def _fake_request(cookies: dict[str, str] | None = None, authorization: str | None = None):
    from starlette.requests import Request

    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "headers": headers, "method": "GET", "path": "/"})


async def test_require_scope_from_request_bearer_token_still_works():
    await auth.upsert_bootstrap_key("writer:read|write:writer-raw-key-2")
    record = await auth.require_scope_from_request(
        _fake_request(authorization="Bearer writer-raw-key-2"), Scope.WRITE
    )
    assert record.name == "writer"


async def test_require_scope_from_request_valid_session_cookie_satisfies_read(monkeypatch):
    from chuk_experiments_server import webauth
    from chuk_experiments_server.config import settings
    from chuk_experiments_server.constants import SESSION_COOKIE_NAME

    monkeypatch.setattr(type(settings), "dashboard_auth_configured", property(lambda self: True))
    token = webauth.create_session_cookie_value(settings.dashboard_allowed_email)
    request = _fake_request(cookies={SESSION_COOKIE_NAME: token})
    assert await auth.require_scope_from_request(request, Scope.READ) is None


async def test_require_scope_from_request_session_cookie_never_satisfies_write(monkeypatch):
    from chuk_experiments_server import webauth
    from chuk_experiments_server.config import settings
    from chuk_experiments_server.constants import SESSION_COOKIE_NAME

    monkeypatch.setattr(type(settings), "dashboard_auth_configured", property(lambda self: True))
    token = webauth.create_session_cookie_value(settings.dashboard_allowed_email)
    request = _fake_request(cookies={SESSION_COOKIE_NAME: token})
    with pytest.raises(auth.AuthError) as exc_info:
        await auth.require_scope_from_request(request, Scope.WRITE)
    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED


async def test_require_scope_from_request_open_access_when_dashboard_auth_not_configured(monkeypatch):
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "dashboard_auth_configured", property(lambda self: False))
    assert await auth.require_scope_from_request(_fake_request(), Scope.READ) is None


async def test_require_scope_from_request_no_credential_is_unauthorized_when_configured(monkeypatch):
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "dashboard_auth_configured", property(lambda self: True))
    with pytest.raises(auth.AuthError) as exc_info:
        await auth.require_scope_from_request(_fake_request(), Scope.READ)
    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED


# --- require_dashboard_role ---------------------------------------------------


async def test_require_dashboard_role_bearer_admin_is_system_operator(write_key):
    identity = await auth.require_dashboard_role(
        _fake_request(authorization=f"Bearer {write_key}"), Scope.ADMIN
    )
    assert identity.email is None
    assert identity.role == Scope.ADMIN
    assert identity.user_id is None


async def test_require_dashboard_role_bearer_non_admin_falls_through_to_unauthorized():
    await auth.upsert_bootstrap_key("readonly:read:readonly-dashboard-key")
    with pytest.raises(auth.AuthError) as exc_info:
        await auth.require_dashboard_role(
            _fake_request(authorization="Bearer readonly-dashboard-key"), Scope.READ
        )
    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED


async def test_require_dashboard_role_active_user_with_sufficient_role():
    from chuk_experiments_server import webauth
    from chuk_experiments_server.constants import SESSION_COOKIE_NAME

    await auth.upsert_bootstrap_user("writer@example.com", Scope.WRITE)
    token = webauth.create_session_cookie_value("writer@example.com")
    identity = await auth.require_dashboard_role(
        _fake_request(cookies={SESSION_COOKIE_NAME: token}), Scope.WRITE
    )
    assert identity.email == "writer@example.com"
    assert identity.role == Scope.WRITE
    assert identity.user_id is not None


async def test_require_dashboard_role_insufficient_role_is_forbidden():
    from chuk_experiments_server import webauth
    from chuk_experiments_server.constants import SESSION_COOKIE_NAME

    await auth.upsert_bootstrap_user("reader2@example.com", Scope.READ)
    token = webauth.create_session_cookie_value("reader2@example.com")
    with pytest.raises(auth.AuthError) as exc_info:
        await auth.require_dashboard_role(_fake_request(cookies={SESSION_COOKIE_NAME: token}), Scope.ADMIN)
    assert exc_info.value.status_code == HTTPStatus.FORBIDDEN


async def test_require_dashboard_role_no_credential_is_unauthorized():
    with pytest.raises(auth.AuthError) as exc_info:
        await auth.require_dashboard_role(_fake_request(), Scope.READ)
    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED


async def test_require_dashboard_role_revoked_user_is_unauthorized():
    from chuk_experiments_server import webauth
    from chuk_experiments_server.constants import SESSION_COOKIE_NAME
    from chuk_experiments_server.db import get_pool

    await auth.upsert_bootstrap_user("revoked@example.com", Scope.ADMIN)
    pool = await get_pool()
    await pool.execute("UPDATE app_user SET revoked_at = now() WHERE email = $1", "revoked@example.com")
    token = webauth.create_session_cookie_value("revoked@example.com")
    with pytest.raises(auth.AuthError) as exc_info:
        await auth.require_dashboard_role(_fake_request(cookies={SESSION_COOKIE_NAME: token}), Scope.READ)
    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED
