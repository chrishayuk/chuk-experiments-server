import httpx

from chuk_experiments_server import internal_client


async def test_get_client_lazily_creates_and_reuses():
    internal_client.set_client(None)
    try:
        first = internal_client.get_client()
        second = internal_client.get_client()
        assert first is second
        assert isinstance(first, httpx.AsyncClient)
    finally:
        await internal_client.close_client()


async def test_set_client_overrides_lazy_creation():
    fake = httpx.AsyncClient()
    internal_client.set_client(fake)
    try:
        assert internal_client.get_client() is fake
    finally:
        await internal_client.close_client()


async def test_close_client_is_a_noop_when_nothing_created():
    internal_client.set_client(None)
    await internal_client.close_client()  # must not raise
