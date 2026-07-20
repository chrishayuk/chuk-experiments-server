import asyncpg

from ..constants import (
    DEFAULT_EXPERIMENT_ORDER,
    DEFAULT_EXPERIMENT_SORT,
    DEFAULT_LIST_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    EXPERIMENT_ID_PREFIX,
    EXPERIMENT_REF_SEQUENCE,
    EXPERIMENT_SORT_COLUMNS,
    MAX_LIST_LIMIT,
    METRIC_OP_SQL,
    SNIPPET_TRUNCATE_CHARS,
    ExperimentStatus,
    MetricOp,
)
from ..db import get_pool
from ..markdown_render import render as render_writeup_html
from ..models import (
    Experiment,
    ExperimentCreate,
    ExperimentSummary,
    ExperimentUpdate,
    IndexEntry,
    ProgrammeCreate,
    SearchHit,
    Writeup,
    WriteupCreate,
)
from ._shared import ConflictError, NotFoundError, ValidationError, _QueryBuilder, _generate_ref
from .artifacts import _ARTIFACT_COLUMNS
from .programmes import get_or_create_programme


def _apply_experiment_filters(
    where: list[str],
    q: _QueryBuilder,
    *,
    programme: str | None,
    status: str | None,
    tags: list[str] | None,
) -> None:
    """The programme/status/tags filters, shared identically by
    list_experiments and search_experiments."""
    if programme:
        where.append(f"p.slug = {q.bind(programme)}")
    if status:
        where.append(f"e.status = {q.bind(status)}")
    if tags:
        where.append(f"e.tags && {q.bind(tags)}::text[]")


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
    q_builder = _QueryBuilder()

    where = ["1=1"]
    _apply_experiment_filters(where, q_builder, programme=programme, status=status, tags=tags)
    if q:
        where.append(f"e.search @@ plainto_tsquery('english', {q_builder.bind(q)})")
    if needs_conclusion:
        where.append(
            f"e.status = {q_builder.bind(ExperimentStatus.COMPLETED.value)} AND e.conclusion IS NULL"
        )
    if needs_next_action:
        where.append(
            f"e.status IN ({q_builder.bind(ExperimentStatus.PLANNED.value)}, "
            f"{q_builder.bind(ExperimentStatus.RUNNING.value)}) AND e.next_action IS NULL"
        )

    limit_param = q_builder.bind(limit)
    offset_param = q_builder.bind(offset)
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
        *q_builder.params,
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

    artifact_rows = await pool.fetch(
        f"SELECT {_ARTIFACT_COLUMNS} FROM artifact WHERE experiment_id = $1 ORDER BY created_at",
        experiment["id"],
    )
    experiment["artifacts"] = [dict(a) for a in artifact_rows]

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
    q_builder = _QueryBuilder()

    where = ["1=1"]
    if query:
        where.append(f"e.search @@ plainto_tsquery('english', {q_builder.bind(query)})")
    _apply_experiment_filters(where, q_builder, programme=programme, status=status, tags=tags)
    if config_key and config_value is not None:
        key_param = q_builder.bind(config_key)
        value_param = q_builder.bind(config_value)
        where.append(
            f"EXISTS (SELECT 1 FROM run r WHERE r.experiment_id = e.id AND r.config->>{key_param} = {value_param})"
        )
    if metric and metric_op and metric_value is not None:
        sql_op = METRIC_OP_SQL[MetricOp(metric_op)]
        metric_param = q_builder.bind(metric)
        value_param = q_builder.bind(metric_value)
        where.append(
            "EXISTS (SELECT 1 FROM result res JOIN run r2 ON r2.id = res.run_id "
            f"WHERE r2.experiment_id = e.id AND res.name = {metric_param} AND res.value {sql_op} {value_param})"
        )

    if query:
        query_param = q_builder.bind(query)
        rank_expr = f"ts_rank(e.search, plainto_tsquery('english', {query_param}))"
        snippet_expr = f"ts_headline('english', coalesce(e.hypothesis, e.title), plainto_tsquery('english', {query_param}))"
        order_by = "rank DESC"
    else:
        rank_expr = "0"
        snippet_expr = f"left(coalesce(e.hypothesis, e.title), {SNIPPET_TRUNCATE_CHARS})"
        order_by = "e.updated_at DESC"

    limit_param = q_builder.bind(limit)
    offset_param = q_builder.bind(offset)
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
        *q_builder.params,
    )
    return [SearchHit.model_validate(dict(row)) for row in rows]


async def get_index(
    limit: int = MAX_LIST_LIMIT, offset: int = 0, programme: str | None = None
) -> tuple[list[IndexEntry], int]:
    """A compact, paginated catalogue (spec §5a) — one row per experiment,
    hypothesis truncated to ~200 chars (full text belongs in
    get_experiment). Originally framed as "the whole catalogue in one call,
    small enough to read in full"; that stopped being true well before ~380
    experiments (250K+ characters, over any reasonable tool-response
    budget), hence the pagination and the `programme` filter — for
    structured filtering beyond that (status, tags, conclusion/next-action
    needed), prefer list_experiments instead; this tool's value is the
    compact hypothesis+headline-metric projection, not filtering power.

    Returns `(rows, total)` — `total` counts every matching experiment
    regardless of `limit`/`offset`, computed separately rather than via a
    window function, since a window function returns nothing to read when
    the requested page itself is empty (e.g. `offset` past the end) —
    exactly when a caller most needs to know how many rows actually exist."""
    pool = await get_pool()
    total_args = [programme] if programme else []
    total_where = "WHERE p.slug = $1" if programme else ""
    total = await pool.fetchval(
        f"SELECT count(*) FROM experiment e JOIN programme p ON p.id = e.programme_id {total_where}",
        *total_args,
    )

    page_where = "WHERE p.slug = $3" if programme else ""
    rows = await pool.fetch(
        f"""
        SELECT e.slug, e.title, e.status, e.tags, left(e.hypothesis, {SNIPPET_TRUNCATE_CHARS}) AS hypothesis,
               p.slug AS programme_slug,
               (
                   SELECT jsonb_build_object('name', res.name, 'value', res.value, 'verdict', res.verdict)
                   FROM result res
                   JOIN run r ON r.id = res.run_id
                   WHERE r.experiment_id = e.id AND res.superseded_by IS NULL
                   ORDER BY res.created_at DESC
                   LIMIT 1
               ) AS headline_metric
        FROM experiment e
        JOIN programme p ON p.id = e.programme_id
        {page_where}
        ORDER BY e.updated_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
        *total_args,
    )
    return [IndexEntry.model_validate(dict(row)) for row in rows], total
