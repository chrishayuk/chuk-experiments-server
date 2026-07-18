"""Shared business logic — every REST endpoint and every MCP tool calls into here.

Keeping DB access in one place is what makes "the server never executes
anything, just records" tractable: this module is the only thing that talks
to Postgres. Every public function takes/returns the Pydantic models from
`models.py`, so both entry points (REST body parsing, MCP tool arguments)
validate once, at the edge, in the same shape.
"""

from datetime import datetime, timezone
from typing import Any

import asyncpg

from .constants import (
    CANCELLABLE_RUN_STATUSES,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_MAX_CLAIM_ATTEMPTS,
    DEFAULT_SEARCH_LIMIT,
    EXPERIMENT_ID_PREFIX,
    EXPERIMENT_REF_SEQUENCE,
    ID_SEQUENCE_PAD_WIDTH,
    LEASABLE_RUN_STATUSES,
    METRIC_OP_SQL,
    RUN_ID_PREFIX,
    RUN_REF_SEQUENCE,
    MetricOp,
    RunStatus,
)
from .db import get_pool
from .markdown_render import render as render_writeup_html
from .models import (
    Artifact,
    ArtifactCreate,
    Experiment,
    ExperimentCreate,
    ExperimentSummary,
    ExperimentUpdate,
    IndexEntry,
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
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> list[ExperimentSummary]:
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

    limit_param = bind(limit)
    offset_param = bind(offset)

    rows = await pool.fetch(
        f"""
        SELECT e.id, e.slug, e.title, e.status, e.tags, e.created_at, e.updated_at,
               p.slug AS programme_slug, p.name AS programme_name
        FROM experiment e
        JOIN programme p ON p.id = e.programme_id
        WHERE {" AND ".join(where)}
        ORDER BY e.updated_at DESC
        LIMIT {limit_param} OFFSET {offset_param}
        """,
        *params,
    )
    return [ExperimentSummary.model_validate(dict(row)) for row in rows]


async def get_experiment(slug: str) -> Experiment:
    pool = await get_pool()
    exp = await pool.fetchrow(
        """
        SELECT e.id, e.slug, e.title, e.status, e.hypothesis, e.design, e.tags,
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
            updated_at = now()
        WHERE slug = $1
        RETURNING slug
        """,
        slug,
        data.status.value if data.status else None,
        data.tags,
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


async def get_index() -> list[IndexEntry]:
    """The full compact catalogue (spec §5a) — small enough that an agent
    reads the whole thing in one call and does semantic matching itself,
    in-context, rather than relying on FTS alone. Expected to be the
    most-used read tool."""
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
        """
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
            "SELECT id, run_id, kind, uri, bytes, sha256, meta, created_at "
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
    pool = await get_pool()
    run_exists = await pool.fetchval("SELECT 1 FROM run WHERE id = $1", run_id)
    if not run_exists:
        raise NotFoundError(f"No run with id {run_id}")
    row = await pool.fetchrow(
        """
        INSERT INTO artifact (run_id, kind, uri, bytes, sha256, meta)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, run_id, kind, uri, bytes, sha256, meta, created_at
        """,
        run_id,
        data.kind.value,
        data.uri,
        data.bytes,
        data.sha256,
        data.meta,
    )
    return Artifact.model_validate(dict(row))


async def get_artifact(artifact_id: int) -> Artifact:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, run_id, kind, uri, bytes, sha256, meta, created_at FROM artifact WHERE id = $1",
        artifact_id,
    )
    if row is None:
        raise NotFoundError(f"No artifact with id {artifact_id}")
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
        SELECT a.id, a.run_id, a.kind, a.uri, a.bytes, a.sha256, a.meta, a.created_at
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
