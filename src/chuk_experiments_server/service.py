"""Shared business logic — every REST endpoint and every MCP tool calls into here.

Keeping DB access in one place is what makes "the server never executes
anything, just records" tractable: this module is the only thing that talks
to Postgres. Every public function takes/returns the Pydantic models from
`models.py`, so both entry points (REST body parsing, MCP tool arguments)
validate once, at the edge, in the same shape.
"""

from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

import asyncpg

from . import external_refs, token_crypto
from .auth import AuthError, generate_key, hash_key
from .config import settings
from .constants import (
    CANCELLABLE_RUN_STATUSES,
    DEFAULT_EXPERIMENT_ORDER,
    DEFAULT_EXPERIMENT_SORT,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_MAX_CLAIM_ATTEMPTS,
    DEFAULT_SEARCH_LIMIT,
    EXPERIMENT_ID_PREFIX,
    EXPERIMENT_REF_SEQUENCE,
    EXPERIMENT_SORT_COLUMNS,
    GIT_URI_PREFIXES,
    HF_URI_PREFIX,
    ID_SEQUENCE_PAD_WIDTH,
    LEASABLE_RUN_STATUSES,
    MAX_LIST_LIMIT,
    METRIC_OP_SQL,
    ROLE_SCOPE_CEILING,
    RUN_ID_PREFIX,
    RUN_REF_SEQUENCE,
    VALID_ARTIFACT_URI_PREFIXES,
    ArtifactRole,
    ExperimentStatus,
    MetricOp,
    RunStatus,
    Scope,
    TokenProvider,
)
from .db import get_pool
from .markdown_render import render as render_writeup_html
from .models import (
    AppUser,
    ApiKeyCreateResponse,
    ApiKeySummary,
    Artifact,
    ArtifactCreate,
    ArtifactLineage,
    ArtifactPin,
    DashboardIdentity,
    Experiment,
    ExperimentCreate,
    ExperimentSummary,
    ExperimentUpdate,
    ExternalRefSummary,
    IndexEntry,
    PinSummary,
    Programme,
    ProgrammeCreate,
    QueueSweepResult,
    Result,
    ResultCreate,
    Run,
    RunComparisonRow,
    RunCreate,
    RunUpdate,
    SearchHit,
    Writeup,
    WriteupCreate,
)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    """Raised when a state transition isn't valid from the run's current status."""


class ValidationError(Exception):
    """Input that parses fine at the Pydantic layer but fails a business-logic
    check the model itself can't express (e.g. register_artifact's uri-scheme
    check) — mapped to 422 by errors.py, same as a pydantic.ValidationError."""


# ---------------------------------------------------------------------------
# Programmes
# ---------------------------------------------------------------------------


async def list_programmes() -> list[Programme]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT p.id, p.slug, p.name, p.description, p.created_at,
               count(e.id) AS experiment_count
        FROM programme p
        LEFT JOIN experiment e ON e.programme_id = p.id
        GROUP BY p.id
        ORDER BY p.slug
        """
    )
    return [Programme.model_validate(dict(row)) for row in rows]


async def get_or_create_programme(data: ProgrammeCreate) -> Programme:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO programme (slug, name, description)
        VALUES ($1, $2, $3)
        ON CONFLICT (slug) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, programme.name),
            description = COALESCE(EXCLUDED.description, programme.description)
        RETURNING id, slug, name, description, created_at
        """,
        data.slug,
        data.name or _humanize_slug(data.slug),
        data.description,
    )
    return Programme.model_validate(dict(row))


def _humanize_slug(slug: str) -> str:
    """Spec has no dedicated programme-create endpoint — a programme is
    implicitly created the first time an experiment references its slug, so
    give it a readable default name ("state-construction" -> "State
    Construction") rather than echoing the slug verbatim."""
    return slug.replace("-", " ").title()


async def _generate_ref(prefix: str, sequence_name: str) -> str:
    """Sortable id/slug: {PREFIX}-{YYYYMMDD}-{HHMMSS}-{zero-padded sequence
    number}, e.g. "RUN-20260718-160217-00397" — matches the format already
    used by the gpu-training-harness train server. Used as experiment.id/
    run.id always, and as their slug when the caller doesn't supply one."""
    pool = await get_pool()
    seq = await pool.fetchval(f"SELECT nextval('{sequence_name}')")
    now = datetime.now(timezone.utc)
    return f"{prefix}-{now:%Y%m%d}-{now:%H%M%S}-{seq:0{ID_SEQUENCE_PAD_WIDTH}d}"


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


async def list_experiments(
    programme: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    q: str | None = None,
    needs_conclusion: bool | None = None,
    needs_next_action: bool | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    sort: str = DEFAULT_EXPERIMENT_SORT,
    order: str = DEFAULT_EXPERIMENT_ORDER,
) -> list[ExperimentSummary]:
    if sort not in EXPERIMENT_SORT_COLUMNS:
        raise ValidationError(f"sort must be one of {sorted(EXPERIMENT_SORT_COLUMNS)}, got '{sort}'")
    if order not in ("asc", "desc"):
        raise ValidationError(f"order must be 'asc' or 'desc', got '{order}'")

    pool = await get_pool()
    params: list[Any] = []

    def bind(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    where = ["1=1"]
    if programme:
        where.append(f"p.slug = {bind(programme)}")
    if status:
        where.append(f"e.status = {bind(status)}")
    if tags:
        where.append(f"e.tags && {bind(tags)}::text[]")
    if q:
        where.append(f"e.search @@ plainto_tsquery('english', {bind(q)})")
    if needs_conclusion:
        where.append(f"e.status = {bind(ExperimentStatus.COMPLETED.value)} AND e.conclusion IS NULL")
    if needs_next_action:
        where.append(
            f"e.status IN ({bind(ExperimentStatus.PLANNED.value)}, {bind(ExperimentStatus.RUNNING.value)}) "
            "AND e.next_action IS NULL"
        )

    limit_param = bind(limit)
    offset_param = bind(offset)
    order_column = EXPERIMENT_SORT_COLUMNS[sort]
    order_direction = order.upper()

    rows = await pool.fetch(
        f"""
        SELECT e.id, e.slug, e.title, e.status, e.tags, e.created_at, e.updated_at,
               p.slug AS programme_slug, p.name AS programme_name
        FROM experiment e
        JOIN programme p ON p.id = e.programme_id
        WHERE {" AND ".join(where)}
        ORDER BY {order_column} {order_direction}, e.id
        LIMIT {limit_param} OFFSET {offset_param}
        """,
        *params,
    )
    return [ExperimentSummary.model_validate(dict(row)) for row in rows]


async def get_research_health() -> dict[str, int]:
    """Counts behind the Overview dashboard's "needs conclusion"/"needs next
    action" tiles — how much of the research record risks going stale."""
    pool = await get_pool()
    needs_conclusion = await pool.fetchval(
        "SELECT COUNT(*) FROM experiment WHERE status = $1 AND conclusion IS NULL",
        ExperimentStatus.COMPLETED.value,
    )
    needs_next_action = await pool.fetchval(
        "SELECT COUNT(*) FROM experiment WHERE status IN ($1, $2) AND next_action IS NULL",
        ExperimentStatus.PLANNED.value,
        ExperimentStatus.RUNNING.value,
    )
    return {"needs_conclusion": needs_conclusion, "needs_next_action": needs_next_action}


async def get_experiment(slug: str) -> Experiment:
    pool = await get_pool()
    exp = await pool.fetchrow(
        """
        SELECT e.id, e.slug, e.title, e.status, e.hypothesis, e.conclusion, e.next_action,
               e.design, e.tags,
               e.created_at, e.updated_at, p.slug AS programme_slug, p.name AS programme_name
        FROM experiment e
        JOIN programme p ON p.id = e.programme_id
        WHERE e.slug = $1
        """,
        slug,
    )
    if exp is None:
        raise NotFoundError(f"No experiment with slug '{slug}'")
    experiment = dict(exp)

    writeup_row = await pool.fetchrow(
        """
        SELECT version, body_md, author, created_at
        FROM writeup WHERE experiment_id = $1
        ORDER BY version DESC LIMIT 1
        """,
        experiment["id"],
    )
    if writeup_row:
        writeup = dict(writeup_row)
        writeup["body_html"] = render_writeup_html(writeup["body_md"])
        experiment["latest_writeup"] = Writeup.model_validate(writeup)
    else:
        experiment["latest_writeup"] = None

    run_rows = await pool.fetch(
        """
        SELECT id, slug, status, backend, wandb_url, started_at, ended_at, cost_usd
        FROM run WHERE experiment_id = $1
        ORDER BY created_at DESC
        """,
        experiment["id"],
    )
    experiment["runs"] = [dict(r) for r in run_rows]

    return Experiment.model_validate(experiment)


async def create_experiment(data: ExperimentCreate) -> Experiment:
    prog = await get_or_create_programme(ProgrammeCreate(slug=data.programme, name=data.programme_name))
    experiment_id = await _generate_ref(EXPERIMENT_ID_PREFIX, EXPERIMENT_REF_SEQUENCE)
    slug = data.slug or await _generate_ref(EXPERIMENT_ID_PREFIX, EXPERIMENT_REF_SEQUENCE)
    pool = await get_pool()
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO experiment (id, programme_id, slug, title, status, hypothesis, design, tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, slug, title, status, hypothesis, design, tags, created_at, updated_at
            """,
            experiment_id,
            prog.id,
            slug,
            data.title,
            data.status.value,
            data.hypothesis,
            data.design,
            data.tags,
        )
    except asyncpg.UniqueViolationError:
        raise ConflictError(f"Experiment '{slug}' already exists in programme '{data.programme}'") from None
    return await get_experiment(row["slug"])


async def update_experiment(slug: str, data: ExperimentUpdate) -> Experiment:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE experiment SET
            status = COALESCE($2, status),
            tags = COALESCE($3, tags),
            conclusion = COALESCE($4, conclusion),
            next_action = COALESCE($5, next_action),
            updated_at = now()
        WHERE slug = $1
        RETURNING slug
        """,
        slug,
        data.status.value if data.status else None,
        data.tags,
        data.conclusion,
        data.next_action,
    )
    if row is None:
        raise NotFoundError(f"No experiment with slug '{slug}'")
    return await get_experiment(row["slug"])


async def append_writeup(slug: str, author: str, data: WriteupCreate) -> Writeup:
    """`author` is the calling API key's identity, not client-supplied — see
    the docstring on submit_result for why."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        exp_id = await conn.fetchval("SELECT id FROM experiment WHERE slug = $1", slug)
        if exp_id is None:
            raise NotFoundError(f"No experiment with slug '{slug}'")
        next_version = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM writeup WHERE experiment_id = $1", exp_id
        )
        row = await conn.fetchrow(
            """
            INSERT INTO writeup (experiment_id, version, body_md, author)
            VALUES ($1, $2, $3, $4)
            RETURNING version, body_md, author, created_at
            """,
            exp_id,
            next_version,
            data.body_md,
            author,
        )
    writeup = dict(row)
    writeup["body_html"] = render_writeup_html(writeup["body_md"])
    return Writeup.model_validate(writeup)


async def search_experiments(
    query: str | None = None,
    programme: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    config_key: str | None = None,
    config_value: str | None = None,
    metric: str | None = None,
    metric_op: str | None = None,
    metric_value: float | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
) -> list[SearchHit]:
    """FTS (`query`) combinable with structured filters (spec §5a): programme/
    status/tags narrow by metadata, `config_key`/`config_value` matches a
    JSONB key on any of the experiment's runs, `metric`/`metric_op`/
    `metric_value` matches a result value on any run (e.g. gsm8k_acc > 0.4).
    `metric_op` is one of MetricOp's values — never interpolated directly,
    only used as a lookup key into METRIC_OP_SQL, so arbitrary SQL can't ride
    in on it.
    """
    pool = await get_pool()
    params: list[Any] = []

    def bind(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    where = ["1=1"]
    if query:
        where.append(f"e.search @@ plainto_tsquery('english', {bind(query)})")
    if programme:
        where.append(f"p.slug = {bind(programme)}")
    if status:
        where.append(f"e.status = {bind(status)}")
    if tags:
        where.append(f"e.tags && {bind(tags)}::text[]")
    if config_key and config_value is not None:
        key_param = bind(config_key)
        value_param = bind(config_value)
        where.append(
            f"EXISTS (SELECT 1 FROM run r WHERE r.experiment_id = e.id AND r.config->>{key_param} = {value_param})"
        )
    if metric and metric_op and metric_value is not None:
        sql_op = METRIC_OP_SQL[MetricOp(metric_op)]
        metric_param = bind(metric)
        value_param = bind(metric_value)
        where.append(
            "EXISTS (SELECT 1 FROM result res JOIN run r2 ON r2.id = res.run_id "
            f"WHERE r2.experiment_id = e.id AND res.name = {metric_param} AND res.value {sql_op} {value_param})"
        )

    if query:
        query_param = bind(query)
        rank_expr = f"ts_rank(e.search, plainto_tsquery('english', {query_param}))"
        snippet_expr = f"ts_headline('english', coalesce(e.hypothesis, e.title), plainto_tsquery('english', {query_param}))"
        order_by = "rank DESC"
    else:
        rank_expr = "0"
        snippet_expr = "left(coalesce(e.hypothesis, e.title), 200)"
        order_by = "e.updated_at DESC"

    limit_param = bind(limit)
    offset_param = bind(offset)
    rows = await pool.fetch(
        f"""
        SELECT e.slug, e.title, e.status, p.slug AS programme_slug,
               {rank_expr} AS rank,
               {snippet_expr} AS snippet
        FROM experiment e
        JOIN programme p ON p.id = e.programme_id
        WHERE {" AND ".join(where)}
        ORDER BY {order_by}
        LIMIT {limit_param} OFFSET {offset_param}
        """,
        *params,
    )
    return [SearchHit.model_validate(dict(row)) for row in rows]


async def get_index(limit: int = MAX_LIST_LIMIT, offset: int = 0) -> list[IndexEntry]:
    """The full compact catalogue (spec §5a) — small enough that an agent
    reads the whole thing in one call and does semantic matching itself,
    in-context, rather than relying on FTS alone. Expected to be the
    most-used read tool. `limit` defaults to MAX_LIST_LIMIT rather than
    DEFAULT_LIST_LIMIT — a bounded full scan, not an unbounded one, but
    still "the whole catalogue in one call" for any realistic experiment
    count today."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT e.slug, e.title, e.status, e.tags, e.hypothesis,
               p.slug AS programme_slug,
               (
                   SELECT jsonb_build_object('name', res.name, 'value', res.value, 'verdict', res.verdict)
                   FROM result res
                   JOIN run r ON r.id = res.run_id
                   WHERE r.experiment_id = e.id
                   ORDER BY res.created_at DESC
                   LIMIT 1
               ) AS headline_metric
        FROM experiment e
        JOIN programme p ON p.id = e.programme_id
        ORDER BY e.updated_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )
    return [IndexEntry.model_validate(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


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
            "SELECT id, run_id, name, value, value_json, verdict, notes, submitted_by, created_at "
            "FROM result WHERE run_id = $1 ORDER BY created_at",
            run_id,
        )
    ]
    data["artifacts"] = [
        dict(a)
        for a in await pool.fetch(
            "SELECT id, run_id, kind, uri, bytes, sha256, meta, created_at, name, role, verify_status, verified_at, verify_detail "
            "FROM artifact WHERE run_id = $1 ORDER BY created_at",
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
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT r.id AS run_id, r.slug AS run_slug, e.slug AS experiment_slug,
               res.value, res.value_json, res.verdict
        FROM run r
        JOIN experiment e ON e.id = r.experiment_id
        LEFT JOIN result res ON res.run_id = r.id AND res.name = $2
        WHERE r.id = ANY($1::text[])
        ORDER BY r.id
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


# ---------------------------------------------------------------------------
# Queue (spec §6a)
# ---------------------------------------------------------------------------

#: A run is "ready" when queued and every dependency has completed. Missing
#: dependency ids (dr.id IS NULL) count as unmet, not as vacuously satisfied.
_READY_CLAUSE = """
    r.status = 'queued'
    AND NOT EXISTS (
        SELECT 1 FROM unnest(r.depends_on) AS dep_id
        LEFT JOIN run dr ON dr.id = dep_id
        WHERE dr.id IS NULL OR dr.status <> 'completed'
    )
"""


async def peek_queue(
    backend: str | None = None, max_seconds: int | None = None, limit: int = DEFAULT_LIST_LIMIT
) -> list[Run]:
    """Read-only view of ready runs — does not claim anything."""
    pool = await get_pool()
    where = [_READY_CLAUSE]
    params: list[Any] = []

    def bind(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if backend:
        backend_param = bind(backend)
        where.append(
            f"(r.requirements->>'backend' IS NULL OR r.requirements->>'backend' IN ('any', {backend_param}))"
        )
    if max_seconds is not None:
        where.append(f"(r.est_seconds IS NULL OR r.est_seconds <= {bind(max_seconds)})")

    limit_param = bind(limit)
    rows = await pool.fetch(
        f"""
        SELECT r.id FROM run r
        WHERE {" AND ".join(where)}
        ORDER BY r.priority DESC, r.created_at
        LIMIT {limit_param}
        """,
        *params,
    )
    return [await get_run(row["id"]) for row in rows]


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

        claimed_ids: list[str] = []
        remaining = session_seconds
        for row in candidates:
            cost = row["est_seconds"] or 0
            if cost > session_seconds:
                continue  # can never fit this session, regardless of what's already claimed
            if cost > remaining:
                continue  # doesn't fit right now — keep scanning for something smaller
            claimed_ids.append(row["id"])
            remaining -= cost
            if remaining <= 0:
                break

        if claimed_ids:
            await conn.execute(
                """
                UPDATE run SET
                    status = 'claimed',
                    claimed_by = $2,
                    claimed_at = now(),
                    lease_expires_at = now() + make_interval(secs => $3)
                WHERE id = ANY($1::text[])
                """,
                claimed_ids,
                claimed_by,
                lease_seconds,
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
    async with pool.acquire() as conn, conn.transaction():
        requeued = await conn.fetch(
            """
            UPDATE run SET
                status = 'queued', claimed_by = NULL, claimed_at = NULL,
                lease_expires_at = NULL, claim_attempts = claim_attempts + 1
            WHERE status IN ('claimed', 'running') AND lease_expires_at < now()
              AND claim_attempts + 1 < $1
            RETURNING id
            """,
            max_attempts,
        )
        lost = await conn.fetch(
            """
            UPDATE run SET status = 'lost'
            WHERE status IN ('claimed', 'running') AND lease_expires_at < now()
              AND claim_attempts + 1 >= $1
            RETURNING id
            """,
            max_attempts,
        )
    return QueueSweepResult(requeued=len(requeued), lost=len(lost))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


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
        """
        INSERT INTO result (run_id, name, value, value_json, verdict, notes, submitted_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, run_id, name, value, value_json, verdict, notes, submitted_by, created_at
        """,
        run_id,
        data.name,
        data.value,
        data.value_json,
        data.verdict.value if data.verdict else None,
        data.notes,
        submitted_by,
    )
    return Result.model_validate(dict(row))


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


async def register_artifact(run_id: str, data: ArtifactCreate) -> Artifact:
    if not data.uri.startswith(VALID_ARTIFACT_URI_PREFIXES):
        raise ValidationError(
            f"Artifact uri '{data.uri}' isn't a real accessible location "
            f"(expected one of {VALID_ARTIFACT_URI_PREFIXES}). Local file bytes go through "
            "upload_artifact_to_drive (small config/log/dataset files) or the R2 presign flow "
            "(POST /v1/runs/{run_id}/artifacts/presign, for large checkpoints) — never a local "
            "file:// path or bare filesystem path, which nobody else can resolve."
        )
    pool = await get_pool()
    run_exists = await pool.fetchval("SELECT 1 FROM run WHERE id = $1", run_id)
    if not run_exists:
        raise NotFoundError(f"No run with id {run_id}")

    async def _insert(role: ArtifactRole) -> Any:
        return await pool.fetchrow(
            """
            INSERT INTO artifact (run_id, kind, uri, bytes, sha256, meta, name, role)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, run_id, kind, uri, bytes, sha256, meta, created_at, name, role, verify_status, verified_at, verify_detail
            """,
            run_id,
            data.kind.value,
            data.uri,
            data.bytes,
            data.sha256,
            data.meta,
            data.name,
            role.value,
        )

    try:
        row = await _insert(data.role)
    except asyncpg.UniqueViolationError:
        # Lost a race to register the same (name, sha256) as PRODUCED —
        # idx_artifact_produced_name_sha_unique means another concurrent
        # upload's insert already committed as PRODUCED first. Register
        # this run's copy as USED instead of erroring, matching what
        # find_artifact_by_name_sha would have found had it run a moment
        # later — this is what keeps get_artifact_lineage from silently
        # dropping either run.
        row = await _insert(ArtifactRole.USED)
    return Artifact.model_validate(dict(row))


async def get_artifact(artifact_id: int) -> Artifact:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, run_id, kind, uri, bytes, sha256, meta, created_at, name, role, verify_status, verified_at, verify_detail "
        "FROM artifact WHERE id = $1",
        artifact_id,
    )
    if row is None:
        raise NotFoundError(f"No artifact with id {artifact_id}")
    return Artifact.model_validate(dict(row))


async def verify_artifact(artifact_id: int, requesting_user_id: int | None = None) -> Artifact:
    """Live-check that a git+/hf:// reference artifact still actually
    resolves — not just well-formed, actually there (see external_refs.py's
    module docstring for why "exists by name" isn't good enough). Writes
    verify_status/verified_at so the result is cached, not re-checked on
    every read (GitHub's unauthenticated API is 60 req/hr).

    requesting_user_id (the calling bearer key's created_by_user_id, from
    rest.py) picks whose GitHub/HF token to use, preferring that user's own
    stored token over the server-wide settings.github_token/huggingface_token
    fallback — a single shared token is the wrong fix for a rate limit that
    should be per-person, not per-server."""
    artifact = await get_artifact(artifact_id)
    if artifact.uri.startswith(GIT_URI_PREFIXES):
        host, owner, repo, commit = external_refs.parse_git_uri(artifact.uri)
        token = await get_user_token(requesting_user_id, TokenProvider.GITHUB) or settings.github_token
        result = await external_refs.verify_git_ref(host, owner, repo, commit, token)
    elif artifact.uri.startswith(HF_URI_PREFIX):
        repo_type, repo_id, revision = external_refs.parse_hf_uri(artifact.uri)
        token = (
            await get_user_token(requesting_user_id, TokenProvider.HUGGINGFACE) or settings.huggingface_token
        )
        result = await external_refs.verify_hf_ref(repo_type, repo_id, revision, artifact.bytes, token)
    else:
        raise ValidationError(
            f"Artifact {artifact_id} isn't a git+/hf:// reference (uri: {artifact.uri!r}) — "
            "verify only applies to those two kinds."
        )

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE artifact SET verify_status = $1, verified_at = now(), verify_detail = $2
        WHERE id = $3
        RETURNING id, run_id, kind, uri, bytes, sha256, meta, created_at, name, role, verify_status, verified_at, verify_detail
        """,
        result.status,
        result.detail,
        artifact_id,
    )
    return Artifact.model_validate(dict(row))


async def find_checkpoints(
    experiment: str | None = None,
    model: str | None = None,
    kind: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[Artifact]:
    pool = await get_pool()
    params: list[Any] = []

    def bind(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    where = ["1=1"]
    if experiment:
        where.append(f"e.slug = {bind(experiment)}")
    if model:
        model_param = bind(model)
        where.append(f"(r.config->>'model' = {model_param} OR e.design->>'model' = {model_param})")
    if kind:
        where.append(f"a.kind = {bind(kind)}")

    limit_param = bind(limit)
    rows = await pool.fetch(
        f"""
        SELECT a.id, a.run_id, a.kind, a.uri, a.bytes, a.sha256, a.meta, a.created_at, a.name, a.role, a.verify_status, a.verified_at, a.verify_detail
        FROM artifact a
        JOIN run r ON r.id = a.run_id
        JOIN experiment e ON e.id = r.experiment_id
        WHERE {" AND ".join(where)}
        ORDER BY a.created_at DESC
        LIMIT {limit_param}
        """,
        *params,
    )
    return [Artifact.model_validate(dict(row)) for row in rows]


async def list_external_ref_artifacts(
    limit: int = DEFAULT_LIST_LIMIT, offset: int = 0
) -> list[ExternalRefSummary]:
    """Every git+/hf:// reference artifact across all experiments — the
    dashboard-wide "what do we point at outside this server" view (item 5,
    2026-07-19 roadmap): a run-detail page only shows one run's artifacts,
    and there was no way to browse "every git/HF reference, and which of
    them have gone stale" without opening runs one at a time."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT a.id, a.run_id, e.slug AS experiment_slug, e.title AS experiment_title,
               a.kind, a.uri, a.name, a.role, a.meta, a.verify_status, a.verified_at,
               a.verify_detail, a.created_at
        FROM artifact a
        JOIN run r ON r.id = a.run_id
        JOIN experiment e ON e.id = r.experiment_id
        WHERE a.uri LIKE 'git+%' OR a.uri LIKE 'hf://%'
        ORDER BY a.created_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )
    return [ExternalRefSummary.model_validate(dict(row)) for row in rows]


async def find_artifact_by_name_sha(name: str, sha256: str) -> Artifact | None:
    """The dedup lookup behind upload_artifact_to_drive — if content with
    this exact (name, sha256) has already been uploaded by an earlier run,
    reuse its uri instead of uploading again. Prefers the PRODUCED row when
    one exists (the original), falling back to any row sharing the pair."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, run_id, kind, uri, bytes, sha256, meta, created_at, name, role, verify_status, verified_at, verify_detail
        FROM artifact
        WHERE name = $1 AND sha256 = $2
        ORDER BY (role = 'produced') DESC, created_at ASC
        LIMIT 1
        """,
        name,
        sha256,
    )
    return Artifact.model_validate(dict(row)) if row else None


async def get_artifact_lineage(artifact_id: int) -> ArtifactLineage:
    """Every artifact sharing this one's (name, sha256) is the same content
    — one PRODUCED it (the original upload), any others USED it (a dedup
    hit from a later run). Falls out of grouping existing rows, no
    separate lineage table needed.

    git+/hf:// reference artifacts never have a sha256 (no bytes were ever
    hashed — the commit/revision in the uri itself is the content address),
    so for those the same grouping happens on (name, uri) instead, matching
    idx_artifact_produced_name_uri_unique's dedup key."""
    artifact = await get_artifact(artifact_id)
    if not artifact.name:
        return ArtifactLineage(produced_by_run_id=None, used_by_run_ids=[])

    pool = await get_pool()
    if artifact.sha256:
        rows = await pool.fetch(
            "SELECT run_id, role FROM artifact WHERE name = $1 AND sha256 = $2 ORDER BY created_at",
            artifact.name,
            artifact.sha256,
        )
    else:
        rows = await pool.fetch(
            "SELECT run_id, role FROM artifact WHERE name = $1 AND uri = $2 AND sha256 IS NULL ORDER BY created_at",
            artifact.name,
            artifact.uri,
        )
    produced_by = next((r["run_id"] for r in rows if r["role"] == "produced"), None)
    used_by = [r["run_id"] for r in rows if r["role"] == "used"]
    return ArtifactLineage(produced_by_run_id=produced_by, used_by_run_ids=used_by)


async def set_pin(name: str, artifact_id: int) -> ArtifactPin:
    pool = await get_pool()
    artifact_exists = await pool.fetchval("SELECT 1 FROM artifact WHERE id = $1", artifact_id)
    if not artifact_exists:
        raise NotFoundError(f"No artifact with id {artifact_id}")
    row = await pool.fetchrow(
        """
        INSERT INTO artifact_pin (name, artifact_id)
        VALUES ($1, $2)
        ON CONFLICT (name) DO UPDATE SET artifact_id = EXCLUDED.artifact_id, updated_at = now()
        RETURNING id, name, artifact_id, updated_at
        """,
        name,
        artifact_id,
    )
    return ArtifactPin.model_validate(dict(row))


async def get_pin(name: str) -> Artifact:
    pool = await get_pool()
    artifact_id = await pool.fetchval("SELECT artifact_id FROM artifact_pin WHERE name = $1", name)
    if artifact_id is None:
        raise NotFoundError(f"No pin named '{name}'")
    return await get_artifact(artifact_id)


async def list_pins() -> list[PinSummary]:
    """Denormalized with just enough of each pin's target artifact (run,
    kind, uri, its own name) that the dashboard can render a pins list in
    one call instead of one lineage-style follow-up request per row."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT p.id, p.name, p.artifact_id, p.updated_at,
               a.run_id, a.kind, a.uri, a.name AS artifact_name
        FROM artifact_pin p
        JOIN artifact a ON a.id = p.artifact_id
        ORDER BY p.name
        """
    )
    return [PinSummary.model_validate(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Users & self-service API keys (dashboard team management)
# ---------------------------------------------------------------------------
# Single seeded 'default' team for now (Chris's call — "saves us refactoring
# later" rather than building multi-team support before it's needed): every
# query below implicitly operates within it. Adding real team-switching later
# means adding a team_id filter here, not a schema change.


async def get_active_user_by_email(email: str) -> AppUser | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, role, created_at, revoked_at FROM app_user WHERE email = $1 AND revoked_at IS NULL",
        email,
    )
    return AppUser.model_validate(dict(row)) if row else None


async def list_team_users() -> list[AppUser]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, email, role, created_at, revoked_at FROM app_user ORDER BY created_at"
    )
    return [AppUser.model_validate(dict(row)) for row in rows]


async def create_user(email: str, role: Scope) -> AppUser:
    pool = await get_pool()
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO app_user (team_id, email, role)
            VALUES ((SELECT id FROM team WHERE slug = 'default'), $1, $2)
            RETURNING id, email, role, created_at, revoked_at
            """,
            email,
            role.value,
        )
    except asyncpg.UniqueViolationError:
        raise ConflictError(f"A user with email '{email}' already exists") from None
    return AppUser.model_validate(dict(row))


async def revoke_user(user_id: int) -> None:
    """Soft-revokes the user and cascades to their own API keys — a removed
    collaborator shouldn't leave live credentials behind. Refuses to revoke
    the last remaining active admin: that would leave the team with no one
    able to sign in and manage users/keys through the dashboard at all
    (short of the bearer-ADMIN CLI escape hatch, which isn't a substitute
    for a real admin user)."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        target = await conn.fetchrow(
            "SELECT role FROM app_user WHERE id = $1 AND revoked_at IS NULL", user_id
        )
        if target is None:
            raise NotFoundError(f"No active user with id {user_id}")

        if target["role"] == Scope.ADMIN.value:
            # FOR UPDATE locks every active admin row for the transaction's
            # duration — a concurrent revoke_user targeting a *different*
            # admin blocks here instead of racing this one on a stale
            # count, which is what let two concurrent revokes both pass
            # the check and leave zero active admins. (Postgres rejects
            # FOR UPDATE combined with an aggregate, hence counting the
            # fetched rows in Python rather than `SELECT count(*) ... FOR
            # UPDATE`.)
            admin_rows = await conn.fetch(
                "SELECT id FROM app_user WHERE role = 'admin' AND revoked_at IS NULL FOR UPDATE"
            )
            remaining_admins = sum(1 for row in admin_rows if row["id"] != user_id)
            if remaining_admins == 0:
                raise ConflictError("Cannot revoke the last remaining admin user")

        await conn.execute("UPDATE app_user SET revoked_at = now() WHERE id = $1", user_id)
        await conn.execute(
            "UPDATE api_key SET revoked_at = now() WHERE created_by_user_id = $1 AND revoked_at IS NULL",
            user_id,
        )


async def list_api_keys(caller: DashboardIdentity) -> list[ApiKeySummary]:
    """Admins (including the bearer-ADMIN "system operator", user_id=None)
    see every key on the team; anyone else sees only their own."""
    pool = await get_pool()
    if caller.role == Scope.ADMIN:
        rows = await pool.fetch(
            """
            SELECT k.id, k.name, k.scopes, k.created_at, k.revoked_at, u.email AS created_by_email
            FROM api_key k
            LEFT JOIN app_user u ON u.id = k.created_by_user_id
            ORDER BY k.created_at DESC
            """
        )
    else:
        rows = await pool.fetch(
            """
            SELECT k.id, k.name, k.scopes, k.created_at, k.revoked_at, u.email AS created_by_email
            FROM api_key k
            LEFT JOIN app_user u ON u.id = k.created_by_user_id
            WHERE k.created_by_user_id = $1
            ORDER BY k.created_at DESC
            """,
            caller.user_id,
        )
    return [ApiKeySummary.model_validate(dict(row)) for row in rows]


async def create_api_key(caller: DashboardIdentity, name: str, scopes: list[Scope]) -> ApiKeyCreateResponse:
    """Self-service key minting — `scopes` is capped at the caller's own role
    ceiling (see ROLE_SCOPE_CEILING), so a "write"-role user can never mint
    themselves an admin-scoped key. Returns the raw key once, same
    "shown only now" contract as the CLI's `keys create`."""
    excess = set(scopes) - ROLE_SCOPE_CEILING[caller.role]
    if excess:
        raise AuthError(
            f"role '{caller.role.value}' cannot mint scope(s): {', '.join(sorted(s.value for s in excess))}",
            status_code=HTTPStatus.FORBIDDEN,
        )
    raw = generate_key()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO api_key (key_hash, name, scopes, team_id, created_by_user_id)
        VALUES ($1, $2, $3, (SELECT id FROM team WHERE slug = 'default'), $4)
        RETURNING id, name, scopes, created_at
        """,
        hash_key(raw),
        name,
        [s.value for s in scopes],
        caller.user_id,
    )
    return ApiKeyCreateResponse.model_validate({**dict(row), "raw_key": raw})


async def revoke_api_key(caller: DashboardIdentity, key_id: int) -> None:
    pool = await get_pool()
    if caller.role == Scope.ADMIN:
        row = await pool.fetchrow(
            "UPDATE api_key SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL RETURNING id",
            key_id,
        )
    else:
        row = await pool.fetchrow(
            """
            UPDATE api_key SET revoked_at = now()
            WHERE id = $1 AND created_by_user_id = $2 AND revoked_at IS NULL
            RETURNING id
            """,
            key_id,
            caller.user_id,
        )
    if row is None:
        raise NotFoundError(f"No api key with id {key_id}")


# ---------------------------------------------------------------------------
# Per-user GitHub/HF tokens (external artifact verification)
# ---------------------------------------------------------------------------
# Human/dashboard-only, same as key self-service — no MCP tool wraps these,
# same reasoning as create_api_key having none: this is a one-time personal
# setup action, not something an agent should be doing on a user's behalf.

_TOKEN_COLUMN: dict[TokenProvider, str] = {
    TokenProvider.GITHUB: "github_token_encrypted",
    TokenProvider.HUGGINGFACE: "huggingface_token_encrypted",
}


async def set_user_token(caller: DashboardIdentity, provider: TokenProvider, raw_token: str) -> None:
    if caller.user_id is None:
        raise ValidationError(
            "Personal tokens require a signed-in dashboard user, not a bearer-admin session."
        )
    if not settings.token_encryption_configured:
        raise ValidationError("TOKEN_ENCRYPTION_KEY is not configured on this server.")
    encrypted = token_crypto.encrypt_token(raw_token)
    pool = await get_pool()
    column = _TOKEN_COLUMN[provider]
    await pool.execute(f"UPDATE app_user SET {column} = $1 WHERE id = $2", encrypted, caller.user_id)  # noqa: S608 - column is a fixed enum-keyed lookup, never caller input


async def clear_user_token(caller: DashboardIdentity, provider: TokenProvider) -> None:
    if caller.user_id is None:
        raise ValidationError(
            "Personal tokens require a signed-in dashboard user, not a bearer-admin session."
        )
    pool = await get_pool()
    column = _TOKEN_COLUMN[provider]
    await pool.execute(f"UPDATE app_user SET {column} = NULL WHERE id = $1", caller.user_id)  # noqa: S608 - column is a fixed enum-keyed lookup, never caller input


async def get_user_token_status(user_id: int | None) -> dict[str, bool]:
    if user_id is None:
        return {"github_token_set": False, "huggingface_token_set": False}
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT github_token_encrypted, huggingface_token_encrypted FROM app_user WHERE id = $1", user_id
    )
    if row is None:
        return {"github_token_set": False, "huggingface_token_set": False}
    return {
        "github_token_set": row["github_token_encrypted"] is not None,
        "huggingface_token_set": row["huggingface_token_encrypted"] is not None,
    }


async def get_user_token(user_id: int | None, provider: TokenProvider) -> str | None:
    """Only used by verify_artifact's token resolution — never exposed over
    REST, unlike get_user_token_status."""
    if user_id is None:
        return None
    pool = await get_pool()
    column = _TOKEN_COLUMN[provider]
    encrypted = await pool.fetchval(f"SELECT {column} FROM app_user WHERE id = $1", user_id)  # noqa: S608 - column is a fixed enum-keyed lookup, never caller input
    return token_crypto.decrypt_token(encrypted) if encrypted else None
