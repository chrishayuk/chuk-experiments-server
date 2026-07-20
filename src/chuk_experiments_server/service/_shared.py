"""Exceptions and small helpers shared across every service submodule —
nothing here has its own external dependencies beyond the package root, so
every other submodule can import from here with no risk of a cycle."""

from datetime import datetime, timezone
from typing import Any

from ..constants import ID_SEQUENCE_PAD_WIDTH
from ..db import get_pool


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    """Raised when a state transition isn't valid from the run's current status."""


class ValidationError(Exception):
    """Input that parses fine at the Pydantic layer but fails a business-logic
    check the model itself can't express (e.g. register_artifact's uri-scheme
    check) — mapped to 422 by errors.py, same as a pydantic.ValidationError."""


class _QueryBuilder:
    """Shared params-list + `$N` placeholder-numbering pair for building a
    dynamic parametrized WHERE clause — every function that assembles one
    from optional filters needs this same (params, bind) pairing."""

    def __init__(self) -> None:
        self.params: list[Any] = []

    def bind(self, value: Any) -> str:
        self.params.append(value)
        return f"${len(self.params)}"


async def _generate_ref(prefix: str, sequence_name: str) -> str:
    """Sortable id/slug: {PREFIX}-{YYYYMMDD}-{HHMMSS}-{zero-padded sequence
    number}, e.g. "RUN-20260718-160217-00397" — matches the format already
    used by the gpu-training-harness train server. Used as experiment.id/
    run.id always, and as their slug when the caller doesn't supply one."""
    pool = await get_pool()
    seq = await pool.fetchval(f"SELECT nextval('{sequence_name}')")
    now = datetime.now(timezone.utc)
    return f"{prefix}-{now:%Y%m%d}-{now:%H%M%S}-{seq:0{ID_SEQUENCE_PAD_WIDTH}d}"
