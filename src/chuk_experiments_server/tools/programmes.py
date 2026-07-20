from typing import Any

from ..server import mcp
from ._shared import _api_request


@mcp.tool
async def list_programmes() -> Any:
    """Enumerate every research programme (e.g. cn, div, larql) with its experiment count."""
    return await _api_request("GET", "/v1/programmes")
