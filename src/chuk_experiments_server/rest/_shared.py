"""Small cross-cutting helpers shared by every rest submodule — response
wrapping, limit/offset parsing, and the uniform error-translation decorator."""

import logging
from functools import wraps
from http import HTTPStatus
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import service
from ..constants import MAX_LIST_LIMIT
from ..errors import error_payload
from ..serialization import to_jsonable

logger = logging.getLogger(__name__)


def _ok(data: Any, status: HTTPStatus = HTTPStatus.OK) -> JSONResponse:
    return JSONResponse(to_jsonable(data), status_code=status.value)


def _parse_limit(raw: str | None, default: int) -> int:
    """A bad `?limit=` (non-numeric, negative) is a 422, not an uncaught
    ValueError falling through to a generic 500 — and a huge one is
    silently clamped to MAX_LIST_LIMIT rather than driving an unbounded
    query as the table grows."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise service.ValidationError(f"limit must be an integer, got '{raw}'") from None
    if value < 0:
        raise service.ValidationError(f"limit must be non-negative, got {value}")
    return min(value, MAX_LIST_LIMIT)


def _parse_offset(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise service.ValidationError(f"offset must be an integer, got '{raw}'") from None
    if value < 0:
        raise service.ValidationError(f"offset must be non-negative, got {value}")
    return value


def _with_error_handling(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
    @wraps(handler)
    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except Exception as exc:  # translated uniformly via error_payload
            status, body = error_payload(exc)
            if status == HTTPStatus.INTERNAL_SERVER_ERROR:
                # NotFoundError/ConflictError/ValidationError etc. are
                # expected control flow with their own 4xx status — only an
                # exception error_payload couldn't map at all is logged
                # here, since that's the case where the real traceback
                # would otherwise vanish behind a bare {"error":
                # "internal_error"} with nothing in the server logs to
                # debug from.
                logger.exception("Unhandled exception in %s %s", request.method, request.url.path)
            return JSONResponse(body, status_code=status.value)

    return wrapped
