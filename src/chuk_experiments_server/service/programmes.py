from ..db import get_pool
from ..models import Programme, ProgrammeCreate


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
