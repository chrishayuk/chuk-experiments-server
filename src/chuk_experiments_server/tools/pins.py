from typing import Any

from ..server import mcp
from ._shared import _api_request


@mcp.tool
async def set_pin(name: str, artifact_id: int) -> Any:
    """Point a named, repointable alias (e.g. "tok-v12-tokenizer:latest" or
    ":best") at a specific artifact — creates the pin if it doesn't exist
    yet, or repoints it if it does. Use get_pin to resolve a pin back to
    its current artifact.

    Args:
        name: Pin name (any string you choose, e.g. "tok-v12-tokenizer:latest")
        artifact_id: The artifact this pin should point at right now
    """
    return await _api_request("PUT", f"/v1/pins/{name}", json={"artifact_id": artifact_id})


@mcp.tool
async def get_pin(name: str) -> Any:
    """Resolve a named pin to its current artifact.

    Args:
        name: Pin name (e.g. "tok-v12-tokenizer:latest")
    """
    return await _api_request("GET", f"/v1/pins/{name}")


@mcp.tool
async def list_pins() -> Any:
    """Every pin, with enough of its current target (run, kind, uri, the
    target artifact's own name) to browse without a get_pin call per row —
    the only way to discover a pin's name today is already knowing it."""
    return await _api_request("GET", "/v1/pins")
