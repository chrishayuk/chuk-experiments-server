"""asyncpg pool, created lazily on first use (no ASGI lifespan hook to bind to)."""

import asyncio
import json
import logging

import asyncpg

from .config import settings
from .constants import DB_POOL_MAX_SIZE, DB_POOL_MIN_SIZE

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Decode json/jsonb columns straight to Python objects instead of raw strings."""
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                logger.info("Creating asyncpg pool")
                _pool = await asyncpg.create_pool(
                    dsn=settings.database_url,
                    min_size=DB_POOL_MIN_SIZE,
                    max_size=DB_POOL_MAX_SIZE,
                    init=_init_connection,
                )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def apply_migrations() -> None:
    """Apply every .sql file in migrations/ in filename order. Idempotent (IF NOT EXISTS / OR REPLACE)."""
    pool = await get_pool()
    sql_files = sorted(settings.migrations_dir.glob("*.sql"))
    if not sql_files:
        raise RuntimeError(f"No migration files found in {settings.migrations_dir}")
    async with pool.acquire() as conn:
        for path in sql_files:
            logger.info(f"Applying migration {path.name}")
            await conn.execute(path.read_text())
