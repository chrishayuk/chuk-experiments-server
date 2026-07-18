from http import HTTPStatus

import pydantic

from chuk_experiments_server.auth import AuthError
from chuk_experiments_server.errors import error_payload
from chuk_experiments_server.models import ExperimentCreate
from chuk_experiments_server.service import ConflictError, NotFoundError


def test_auth_error_uses_its_own_status_code():
    status, body = error_payload(AuthError("nope", status_code=HTTPStatus.FORBIDDEN))
    assert status == HTTPStatus.FORBIDDEN
    assert body == {"error": "nope"}


def test_not_found_error_maps_to_404():
    status, body = error_payload(NotFoundError("no such experiment"))
    assert status == HTTPStatus.NOT_FOUND
    assert body == {"error": "no such experiment"}


def test_conflict_error_maps_to_409():
    status, body = error_payload(ConflictError("already exists"))
    assert status == HTTPStatus.CONFLICT


def test_pydantic_validation_error_maps_to_422_with_details():
    try:
        ExperimentCreate.model_validate({})
    except pydantic.ValidationError as exc:
        status, body = error_payload(exc)
    assert status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert body["error"] == "validation_error"
    assert body["detail"]


def test_unexpected_exception_maps_to_500_without_leaking_details():
    status, body = error_payload(RuntimeError("some internal secret detail"))
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert body == {"error": "internal_error"}
