"""Internal HTTP client for calling this server's own REST API — used by
tools.py (MCP tools) and web.py (dashboard) so every consumer goes through
the exact same code path as an external REST client, rather than each
surface reaching into service.py with its own calling convention.

Loopback by default: MCP tools and dashboard routes run in the same process
as the REST endpoints, so this targets 127.0.0.1 on this process's own port
(see cli.py's `_serve`, which sets INTERNAL_API_BASE_URL to match the actual
bound port) rather than a public URL.
"""

import httpx

from .config import settings

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=settings.internal_api_base_url, timeout=30.0)
    return _client


def set_client(client: httpx.AsyncClient | None) -> None:
    """Override the lazily-created client outright — tests use this to point
    it at an in-process ASGI transport instead of a real socket."""
    global _client
    _client = client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
