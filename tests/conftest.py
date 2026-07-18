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

_TABLES = ("artifact", "result", "run", "writeup", "experiment", "programme", "api_key")


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
