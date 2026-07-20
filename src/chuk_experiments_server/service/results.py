from ..db import get_pool
from ..models import Result, ResultCreate
from ._shared import NotFoundError, ValidationError

_RESULT_COLUMNS = (
    "id, run_id, name, value, value_json, verdict, notes, submitted_by, created_at, superseded_by"
)


async def submit_result(run_id: str, submitted_by: str, data: ResultCreate) -> Result:
    """`submitted_by` is the calling API key's identity, not client-supplied —
    the spec's provenance guarantee ("submitted_by gives provenance on every
    result") only holds if the caller can't just put whatever name they like
    in the request body."""
    pool = await get_pool()
    run_exists = await pool.fetchval("SELECT 1 FROM run WHERE id = $1", run_id)
    if not run_exists:
        raise NotFoundError(f"No run with id {run_id}")
    row = await pool.fetchrow(
        f"""
        INSERT INTO result (run_id, name, value, value_json, verdict, notes, submitted_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING {_RESULT_COLUMNS}
        """,
        run_id,
        data.name,
        data.value,
        data.value_json,
        data.verdict.value if data.verdict else None,
        data.notes,
        submitted_by,
    )
    result = Result.model_validate(dict(row))
    if data.supersedes is not None:
        await mark_result_superseded(data.supersedes, result.id)
    return result


async def mark_result_superseded(result_id: int, superseded_by: int) -> Result:
    """Link an existing result as superseded by a later, corrected one — the
    standalone form, for marking this retroactively once you realize an
    older result was wrong. `submit_result`'s `supersedes` param is sugar for
    calling this immediately after inserting the correction. Both ids must
    already exist; self-supersession is rejected before it ever reaches the
    DB's own CHECK constraint, for a clean error instead of a raw one."""
    if result_id == superseded_by:
        raise ValidationError("a result cannot supersede itself")
    pool = await get_pool()
    superseder_exists = await pool.fetchval("SELECT 1 FROM result WHERE id = $1", superseded_by)
    if not superseder_exists:
        raise NotFoundError(f"No result with id {superseded_by}")
    row = await pool.fetchrow(
        f"""
        UPDATE result SET superseded_by = $2 WHERE id = $1
        RETURNING {_RESULT_COLUMNS}
        """,
        result_id,
        superseded_by,
    )
    if row is None:
        raise NotFoundError(f"No result with id {result_id}")
    return Result.model_validate(dict(row))
