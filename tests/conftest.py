"""Test fixtures.

Integration-style: tests run against a real Postgres (the same
docker-compose instance used for local dev), not a mocked DB — service.py is
thin SQL, there's no ORM layer worth mocking, and the interesting bugs here
(unique-violation handling, the queue's dependency-readiness SQL, atomic
claim) only show up against a real database.

A dedicated `chuk_experiments_test` database is created fresh each session
and truncated between tests, so this never touches the `experiments`
database real seed data lives in.
"""

import asyncio
import os

import asyncpg
import pytest

#: Override for CI, where Postgres is a service container on the standard
#: port rather than the local docker-compose instance on 5433.
_ADMIN_DSN = os.environ.get(
    "TEST_DATABASE_ADMIN_URL", "postgresql://experiments:experiments@localhost:5433/experiments"
)
_TEST_DB_NAME = "chuk_experiments_test"
_TEST_DSN = _ADMIN_DSN.rsplit("/", 1)[0] + f"/{_TEST_DB_NAME}"

#: `team` is deliberately NOT truncated — it's seeded once by the migration
#: (a single 'default' row) and never touched per-test, same as the schema
#: itself; truncating it here would wipe that seed and break every
#: team_id-FK insert (api_key, app_user) for the rest of the session.
_TABLES = ("artifact", "result", "run", "writeup", "experiment", "programme", "api_key", "app_user")


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Create a fresh test database and point DATABASE_URL at it, all via
    short-lived connections that never touch db.get_pool() — so the pool
    our app code caches is created fresh, inside pytest-asyncio's session
    loop, on first real use."""

    async def _create() -> None:
        admin_conn = await asyncpg.connect(_ADMIN_DSN)
        try:
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}" WITH (FORCE)')
            await admin_conn.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
        finally:
            await admin_conn.close()

    asyncio.run(_create())
    os.environ["DATABASE_URL"] = _TEST_DSN


@pytest.fixture(scope="session", autouse=True)
async def _apply_schema(_test_database):
    from chuk_experiments_server.db import apply_migrations, close_pool

    await apply_migrations()
    yield
    await close_pool()


@pytest.fixture(autouse=True)
async def _clean_tables(_apply_schema):
    from chuk_experiments_server.db import get_pool

    pool = await get_pool()
    await pool.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
async def write_key(_clean_tables):
    """An API key with read+write+admin scope, for tests that need auth
    context — most service.py tests call service functions directly and
    don't need this, but auth.py tests do. Depends explicitly on
    `_clean_tables` so the key is created *after* the per-test truncate,
    not before it (autouse fixture ordering isn't otherwise guaranteed)."""
    from chuk_experiments_server import auth

    raw = "test-key-" + auth.generate_key()
    await auth.upsert_bootstrap_key(f"pytest:read|write|admin:{raw}")
    return raw


@pytest.fixture(scope="session")
def asgi_app(_apply_schema):
    """The real Starlette app built from whatever's registered in
    chuk_mcp_server's endpoint registry at this point — importing rest.py
    and web.py (whose @mcp.endpoint decorators fire at module-import time,
    same two modules cli.py's _register_rest_routes imports) is what
    populates it. No mcp.run(): that starts a real uvicorn listener, which
    tests don't need — ASGI-transport requests below drive the exact same
    routing/handler code in-process."""
    from starlette.applications import Starlette

    from chuk_experiments_server import rest, web  # noqa: F401 - import registers @mcp.endpoint routes
    from chuk_mcp_server.endpoint_registry import http_endpoint_registry

    return Starlette(routes=http_endpoint_registry.get_routes())


@pytest.fixture
async def api_client(asgi_app):
    """An httpx client wired to the in-process ASGI app instead of a real
    socket — used both for testing rest.py directly and as the transport
    tools.py's internal client is pointed at in tests (see test_tools.py)."""
    import httpx

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def tool_caller(api_client, write_key, monkeypatch):
    """Wires internal_client (what tools.py forwards through) to the
    in-process ASGI transport, and fakes auth.bearer_from_mcp_context() to
    return `write_key` — standing in for chuk_mcp_server's ambient HTTP
    context, which a plain pytest call doesn't have. Yields the raw key in
    case a test wants to reference it (e.g. asserting submitted_by)."""
    from chuk_experiments_server import auth, internal_client

    internal_client.set_client(api_client)
    monkeypatch.setattr(auth, "bearer_from_mcp_context", lambda: write_key)
    yield write_key
    internal_client.set_client(None)


#: Deterministic dashboard-auth config for the whole session, regardless of
#: whatever's (or isn't) in the real project .env — real Google credentials
#: aren't needed since webauth.exchange_code_for_email is mocked in tests
#: that exercise the OAuth callback, not called against the real Google API.
_TEST_ALLOWED_EMAIL = "chrishayuk@googlemail.com"


@pytest.fixture(scope="session", autouse=True)
def _dashboard_auth_env():
    os.environ.setdefault("SESSION_SECRET", "test-session-secret")
    os.environ.setdefault("DASHBOARD_ALLOWED_EMAIL", _TEST_ALLOWED_EMAIL)
    os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
    os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
    os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://test/auth/callback")


@pytest.fixture(autouse=True)
async def _dashboard_admin_user(_clean_tables, _dashboard_auth_env):
    """Seeds the bootstrap admin app_user row after each truncate (mirrors
    write_key) — authenticated_cookies signs a cookie for _TEST_ALLOWED_EMAIL,
    and without a matching active app_user row every dashboard-identity check
    (require_scope_from_request's cookie fallback, web.py's shell gate,
    require_dashboard_role) would treat it as unauthenticated/revoked."""
    from chuk_experiments_server import auth
    from chuk_experiments_server.constants import Scope

    await auth.upsert_bootstrap_user(_TEST_ALLOWED_EMAIL, Scope.ADMIN)


@pytest.fixture
async def dashboard_client(api_client, write_key, monkeypatch):
    """Wires internal_client (what web.py forwards through) to the
    in-process ASGI transport, and sets INTERNAL_API_KEY to a real
    write-scoped key so the dashboard's own REST calls authenticate."""
    from chuk_experiments_server import internal_client

    monkeypatch.setenv("INTERNAL_API_KEY", write_key)
    internal_client.set_client(api_client)
    yield api_client
    internal_client.set_client(None)


@pytest.fixture
def authenticated_cookies():
    from chuk_experiments_server import webauth
    from chuk_experiments_server.constants import SESSION_COOKIE_NAME

    return {SESSION_COOKIE_NAME: webauth.create_session_cookie_value(_TEST_ALLOWED_EMAIL)}
