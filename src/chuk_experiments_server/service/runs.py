import asyncpg

from ..constants import (
    CANCELLABLE_RUN_STATUSES,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_MAX_CLAIM_ATTEMPTS,
    LEASABLE_RUN_STATUSES,
    RUN_ID_PREFIX,
    RUN_REF_SEQUENCE,
    RunStatus,
)
from ..db import get_pool
from ..models import QueueSweepResult, Run, RunComparisonRow, RunCreate, RunUpdate
from ._shared import ConflictError, NotFoundError, _QueryBuilder, _generate_ref
from .artifacts import _ARTIFACT_COLUMNS
from .results import _RESULT_COLUMNS


async def enqueue_run(data: RunCreate) -> Run:
    """Enqueues a run (spec §6a) — `status` defaults to queued but callers
    that already know the run is running/completed (e.g. backfilling
    historical experiments) can set it directly at creation."""
    pool = await get_pool()
    exp_id = await pool.fetchval("SELECT id FROM experiment WHERE slug = $1", data.experiment)
    if exp_id is None:
        raise NotFoundError(f"No experiment with slug '{data.experiment}'")
    run_id = await _generate_ref(RUN_ID_PREFIX, RUN_REF_SEQUENCE)
    slug = data.slug or await _generate_ref(RUN_ID_PREFIX, RUN_REF_SEQUENCE)
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO run (
                id, experiment_id, slug, status, backend, config, budget_seconds,
                priority, depends_on, workspec, requirements, est_seconds
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING id
            """,
            run_id,
            exp_id,
            slug,
            data.status.value,
            data.backend,
            data.config,
            data.budget_seconds,
            data.priority,
            data.depends_on,
            data.workspec,
            data.requirements,
            data.est_seconds,
        )
    except asyncpg.UniqueViolationError:
        raise ConflictError(f"Run '{slug}' already exists on experiment '{data.experiment}'") from None
    return await get_run(row["id"])


_RUN_COLUMNS = """
    r.id, r.slug, r.status, r.priority, r.depends_on, r.workspec, r.requirements,
    r.est_seconds, r.claimed_by, r.claimed_at, r.lease_expires_at, r.claim_attempts,
    r.backend, r.harness_session_id, r.wandb_url,
    r.config, r.started_at, r.ended_at, r.budget_seconds, r.cost_usd, r.created_at,
    e.slug AS experiment_slug, e.title AS experiment_title
"""


async def get_run(run_id: str) -> Run:
    pool = await get_pool()
    run = await pool.fetchrow(
        f"""
        SELECT {_RUN_COLUMNS}
        FROM run r
        JOIN experiment e ON e.id = r.experiment_id
        WHERE r.id = $1
        """,
        run_id,
    )
    if run is None:
        raise NotFoundError(f"No run with id {run_id}")
    data = dict(run)
    data["results"] = [
        dict(r)
        for r in await pool.fetch(
            f"SELECT {_RESULT_COLUMNS} FROM result WHERE run_id = $1 ORDER BY created_at",
            run_id,
        )
    ]
    data["artifacts"] = [
        dict(a)
        for a in await pool.fetch(
            f"SELECT {_ARTIFACT_COLUMNS} FROM artifact WHERE run_id = $1 ORDER BY created_at",
            run_id,
        )
    ]
    return Run.model_validate(data)


async def update_run(run_id: str, data: RunUpdate) -> Run:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE run SET
            status = COALESCE($2, status),
            wandb_url = COALESCE($3, wandb_url),
            harness_session_id = COALESCE($4, harness_session_id),
            started_at = COALESCE($5, started_at),
            ended_at = COALESCE($6, ended_at),
            cost_usd = COALESCE($7, cost_usd)
        WHERE id = $1
        RETURNING id
        """,
        run_id,
        data.status.value if data.status else None,
        data.wandb_url,
        data.harness_session_id,
        data.started_at,
        data.ended_at,
        data.cost_usd,
    )
    if row is None:
        raise NotFoundError(f"No run with id {run_id}")
    return await get_run(run_id)


async def compare_runs(run_ids: list[str], metric: str) -> list[RunComparisonRow]:
    """One row per requested run, always. `found` distinguishes "this run has
    no current result under `metric`" from "it does, and the value/verdict
    happen to be null" — a bare LEFT JOIN can't tell those apart, and an
    agent asking for a metric that doesn't exist deserves an honest signal
    rather than an indistinguishable null row.

    `superseded_by IS NULL` in the join excludes corrected-away results, and
    `DISTINCT ON (r.id)` + `ORDER BY ... created_at DESC` picks the newest
    current match if `result.name` was ever submitted more than once for the
    same run (exactly the corrected-result scenario, since nothing enforces
    uniqueness on (run_id, name))."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (r.id)
               r.id AS run_id, r.slug AS run_slug, e.slug AS experiment_slug,
               res.value, res.value_json, res.verdict, (res.id IS NOT NULL) AS found
        FROM run r
        JOIN experiment e ON e.id = r.experiment_id
        LEFT JOIN result res
            ON res.run_id = r.id AND res.name = $2 AND res.superseded_by IS NULL
        WHERE r.id = ANY($1::text[])
        ORDER BY r.id, res.created_at DESC NULLS LAST
        """,
        run_ids,
        metric,
    )
    return [RunComparisonRow.model_validate(dict(row)) for row in rows]


async def cancel_run(run_id: str) -> Run:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE run SET status = $2
        WHERE id = $1 AND status = ANY($3::text[])
        RETURNING id
        """,
        run_id,
        RunStatus.CANCELLED.value,
        [s.value for s in CANCELLABLE_RUN_STATUSES],
    )
    if row is None:
        raise ConflictError(f"Run {run_id} is not queued/claimed (or does not exist) — cannot cancel")
    return await get_run(run_id)


#: A run is "ready" when queued and every dependency has completed. Missing
#: dependency ids (dr.id IS NULL) count as unmet, not as vacuously satisfied.
_READY_CLAUSE = f"""
    r.status = '{RunStatus.QUEUED.value}'
    AND NOT EXISTS (
        SELECT 1 FROM unnest(r.depends_on) AS dep_id
        LEFT JOIN run dr ON dr.id = dep_id
        WHERE dr.id IS NULL OR dr.status <> '{RunStatus.COMPLETED.value}'
    )
"""


async def peek_queue(
    backend: str | None = None, max_seconds: int | None = None, limit: int = DEFAULT_LIST_LIMIT
) -> list[Run]:
    """Read-only view of ready runs — does not claim anything."""
    pool = await get_pool()
    where = [_READY_CLAUSE]
    q_builder = _QueryBuilder()

    if backend:
        backend_param = q_builder.bind(backend)
        where.append(
            f"(r.requirements->>'backend' IS NULL OR r.requirements->>'backend' IN ('any', {backend_param}))"
        )
    if max_seconds is not None:
        where.append(f"(r.est_seconds IS NULL OR r.est_seconds <= {q_builder.bind(max_seconds)})")

    limit_param = q_builder.bind(limit)
    rows = await pool.fetch(
        f"""
        SELECT r.id FROM run r
        WHERE {" AND ".join(where)}
        ORDER BY r.priority DESC, r.created_at
        LIMIT {limit_param}
        """,
        *q_builder.params,
    )
    return [await get_run(row["id"]) for row in rows]


def _pack_runs_by_session_budget(candidates: list[tuple[str, int | None]], session_seconds: int) -> list[str]:
    """Greedy bin-packing over already priority/age-sorted candidates: claim
    in order until session_seconds runs out, skipping anything that doesn't
    fit right now but keeping smaller later candidates in play. Pure and
    DB-free — the packing decision itself is unit-testable apart from the
    transaction/row-locking claim_queue wraps it in."""
    claimed_ids: list[str] = []
    remaining = session_seconds
    for run_id, est_seconds in candidates:
        cost = est_seconds or 0
        if cost > session_seconds:
            continue  # can never fit this session, regardless of what's already claimed
        if cost > remaining:
            continue  # doesn't fit right now — keep scanning for something smaller
        claimed_ids.append(run_id)
        remaining -= cost
        if remaining <= 0:
            break
    return claimed_ids


async def claim_queue(
    backend: str,
    session_seconds: int,
    claimed_by: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[Run]:
    """Atomically claim as many ready runs as fit `session_seconds`
    (greedy by priority, then by whatever still fits), using
    `FOR UPDATE SKIP LOCKED` so concurrent workers never claim the same run
    twice."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        candidates = await conn.fetch(
            f"""
            SELECT r.id, r.est_seconds FROM run r
            WHERE {_READY_CLAUSE}
              AND (r.requirements->>'backend' IS NULL OR r.requirements->>'backend' IN ('any', $1))
            ORDER BY r.priority DESC, r.created_at
            FOR UPDATE OF r SKIP LOCKED
            """,
            backend,
        )

        claimed_ids = _pack_runs_by_session_budget(
            [(row["id"], row["est_seconds"]) for row in candidates], session_seconds
        )

        if claimed_ids:
            await conn.execute(
                """
                UPDATE run SET
                    status = $4,
                    claimed_by = $2,
                    claimed_at = now(),
                    lease_expires_at = now() + make_interval(secs => $3)
                WHERE id = ANY($1::text[])
                """,
                claimed_ids,
                claimed_by,
                lease_seconds,
                RunStatus.CLAIMED.value,
            )

    return [await get_run(run_id) for run_id in claimed_ids]


async def renew_lease(run_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Run:
    """The heartbeat call — also transitions claimed -> running on first
    renewal, since a lease renewal is the harness telling us the run is
    alive."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE run SET
            lease_expires_at = now() + make_interval(secs => $2),
            status = CASE WHEN status = $3 THEN $4 ELSE status END
        WHERE id = $1 AND status = ANY($5::text[])
        RETURNING id
        """,
        run_id,
        lease_seconds,
        RunStatus.CLAIMED.value,
        RunStatus.RUNNING.value,
        [s.value for s in LEASABLE_RUN_STATUSES],
    )
    if row is None:
        raise ConflictError(f"Run {run_id} is not claimed/running (or does not exist) — cannot renew lease")
    return await get_run(run_id)


async def sweep_expired_leases(max_attempts: int = DEFAULT_MAX_CLAIM_ATTEMPTS) -> QueueSweepResult:
    """Meant to run on a schedule (see cli.py `sweep` / REST `POST
    /v1/queue/sweep`, admin scope): a claimed/running run whose lease expired
    goes back to `queued` for another worker to pick up, unless it's already
    failed `max_attempts` times, in which case it's marked `lost`."""
    pool = await get_pool()
    leasable = [s.value for s in LEASABLE_RUN_STATUSES]
    async with pool.acquire() as conn, conn.transaction():
        requeued = await conn.fetch(
            """
            UPDATE run SET
                status = $2, claimed_by = NULL, claimed_at = NULL,
                lease_expires_at = NULL, claim_attempts = claim_attempts + 1
            WHERE status = ANY($3::text[]) AND lease_expires_at < now()
              AND claim_attempts + 1 < $1
            RETURNING id
            """,
            max_attempts,
            RunStatus.QUEUED.value,
            leasable,
        )
        lost = await conn.fetch(
            """
            UPDATE run SET status = $2
            WHERE status = ANY($3::text[]) AND lease_expires_at < now()
              AND claim_attempts + 1 >= $1
            RETURNING id
            """,
            max_attempts,
            RunStatus.LOST.value,
            leasable,
        )
    return QueueSweepResult(requeued=len(requeued), lost=len(lost))
