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
