"""Small cross-cutting helpers shared by every tools submodule — the
internal-REST-call wrapper and the response-shaping helpers every tool group
reuses."""

from typing import Any

import httpx

from .. import auth, internal_client


async def _api_request(method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
    """Forward to `path` on this server's own REST API, using the calling
    agent's own bearer token. Never raises — a transport-level failure
    (the internal loopback call itself failing) becomes an error dict, same
    shape as errors.error_payload produces for REST/other tools."""
    token = auth.bearer_from_mcp_context()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = await internal_client.get_client().request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        return {"error": f"internal_request_failed: {exc}"}
    try:
        return resp.json()
    except ValueError:
        return {"error": "internal_response_not_json"}


def _query_params(**kwargs: Any) -> dict[str, Any]:
    """Drop None values — httpx would otherwise send them as the literal
    string 'None' in the query string."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _drop_body_html(data: Any) -> Any:
    """Strip the always-computed, never-stored body_html from a write-up
    payload before it reaches an agent — every write-up read/append path
    returns body_md and body_html unconditionally, but body_html is only
    ever useful to the dashboard's own renderer (which calls REST directly),
    so returning it here is pure token waste on the MCP path."""
    if not isinstance(data, dict):
        return data
    data = {k: v for k, v in data.items() if k != "body_html"}
    if isinstance(data.get("latest_writeup"), dict):
        data["latest_writeup"] = {k: v for k, v in data["latest_writeup"].items() if k != "body_html"}
    return data


def _listing(items: list[Any], empty_message: str, *, total: int | None = None) -> dict[str, Any]:
    """Wrap a list-returning tool's response so an empty result is
    self-describing (0 count + a reason) instead of a bare [] that reads as
    ambiguous between "nothing exists", "wrong query", and "tool failure" —
    a real failure mode an agent hit more than once in one session."""
    result: dict[str, Any] = {"results": items, "count": len(items)}
    if total is not None:
        result["total"] = total
    if not items:
        result["message"] = empty_message
    return result
