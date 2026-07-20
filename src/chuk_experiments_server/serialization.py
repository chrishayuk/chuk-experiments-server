"""Pydantic-model -> plain-JSON conversion shared by the REST and MCP surfaces."""

from typing import Any

from pydantic import BaseModel


def to_jsonable(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if isinstance(data, list):
        return [to_jsonable(item) for item in data]
    if isinstance(data, dict):
        return {key: to_jsonable(value) for key, value in data.items()}
    return data
