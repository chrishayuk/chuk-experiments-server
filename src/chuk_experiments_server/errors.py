"""Error -> transport mapping, shared by the REST layer (HTTP responses) and
the MCP tool layer (tool-call error payloads)."""

from http import HTTPStatus
from typing import Any

import pydantic

from .auth import AuthError
from .service import ConflictError, NotFoundError, ValidationError


def error_payload(exc: Exception) -> tuple[HTTPStatus, dict[str, Any]]:
    """Map a raised exception to (status, json body). Shared so REST and MCP
    tools report the same error shape for the same failure."""
    if isinstance(exc, AuthError):
        return exc.status_code, {"error": exc.message}
    if isinstance(exc, NotFoundError):
        return HTTPStatus.NOT_FOUND, {"error": str(exc)}
    if isinstance(exc, ConflictError):
        return HTTPStatus.CONFLICT, {"error": str(exc)}
    if isinstance(exc, ValidationError):
        return HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)}
    if isinstance(exc, pydantic.ValidationError):
        return HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "validation_error", "detail": exc.errors()}
    return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"}
