from typing import Any

import asyncpg

from .. import external_refs
from ..config import settings
from ..constants import (
    ARTIFACT_EXACTLY_ONE_PARENT_ERROR,
    DEFAULT_LIST_LIMIT,
    GIT_URI_PREFIXES,
    HF_URI_PREFIX,
    VALID_ARTIFACT_URI_PREFIXES,
    ArtifactKind,
    ArtifactRole,
    TokenProvider,
)
from ..db import get_pool
from ..models import (
    Artifact,
    ArtifactCreate,
    ArtifactLineage,
    ArtifactPin,
    ExternalRefSummary,
    PinSummary,
)
from ._shared import NotFoundError, ValidationError, _QueryBuilder
from .users import get_user_token

_ARTIFACT_COLUMN_NAMES = (
    "id",
    "run_id",
    "experiment_id",
    "kind",
    "uri",
    "bytes",
    "sha256",
    "meta",
    "created_at",
    "name",
    "role",
    "verify_status",
    "verified_at",
    "verify_detail",
)
_ARTIFACT_COLUMNS = ", ".join(_ARTIFACT_COLUMN_NAMES)


def _artifact_columns(alias: str) -> str:
    """Same columns as _ARTIFACT_COLUMNS, qualified with a table alias — for
    queries that JOIN artifact against other tables sharing column names
    (e.g. created_at), where an unqualified SELECT would be ambiguous."""
    return ", ".join(f"{alias}.{name}" for name in _ARTIFACT_COLUMN_NAMES)


async def register_artifact(
    data: ArtifactCreate, *, run_id: str | None = None, experiment_slug: str | None = None
) -> Artifact:
    """Register a pointer artifact against exactly one parent — a run (the
    common case) or, for provenance that exists before any run does (e.g. a
    pre-registration document), an experiment directly (by slug, resolved to
    its real id here, matching enqueue_run's own convention)."""
    if (run_id is None) == (experiment_slug is None):
        raise ValidationError(ARTIFACT_EXACTLY_ONE_PARENT_ERROR)
    if not data.uri.startswith(VALID_ARTIFACT_URI_PREFIXES):
        raise ValidationError(
            f"Artifact uri '{data.uri}' isn't a real accessible location "
            f"(expected one of {VALID_ARTIFACT_URI_PREFIXES}). Local file bytes go through "
            "upload_artifact_to_drive (small config/log/dataset files) or the R2 presign flow "
            "(POST /v1/runs/{run_id}/artifacts/presign, for large checkpoints) — never a local "
            "file:// path or bare filesystem path, which nobody else can resolve."
        )
    pool = await get_pool()
    experiment_id = None
    if run_id is not None:
        parent_exists = await pool.fetchval("SELECT 1 FROM run WHERE id = $1", run_id)
        if not parent_exists:
            raise NotFoundError(f"No run with id {run_id}")
    else:
        experiment_id = await pool.fetchval("SELECT id FROM experiment WHERE slug = $1", experiment_slug)
        if experiment_id is None:
            raise NotFoundError(f"No experiment with slug '{experiment_slug}'")

    async def _insert(role: ArtifactRole) -> Any:
        return await pool.fetchrow(
            f"""
            INSERT INTO artifact (run_id, experiment_id, kind, uri, bytes, sha256, meta, name, role)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING {_ARTIFACT_COLUMNS}
            """,
            run_id,
            experiment_id,
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


async def register_git_artifact(
    owner: str,
    repo: str,
    commit: str,
    *,
    kind: str = "other",
    name: str | None = None,
    meta: dict[str, Any] | None = None,
    run_id: str | None = None,
    experiment_slug: str | None = None,
) -> Artifact:
    """Register that a run's/experiment's harness/code IS a git commit — no
    bytes move, this just records `git+https://github.com/{owner}/{repo}@{commit}`
    with `meta.git_repo`/`meta.git_commit` set (winning over any caller-supplied
    values of the same keys). A real service function, not just an MCP-side
    convenience, so a REST-only caller gets the same feature — previously this
    URI-building/meta-override logic existed only in tools.py."""
    uri = external_refs.build_git_uri(owner, repo, commit)
    computed_meta = {**(meta or {}), "git_repo": f"{owner}/{repo}", "git_commit": commit}
    data = ArtifactCreate(kind=ArtifactKind(kind), uri=uri, name=name, meta=computed_meta)
    return await register_artifact(data, run_id=run_id, experiment_slug=experiment_slug)


async def register_hf_artifact(
    repo_id: str,
    *,
    revision: str = "main",
    repo_type: str = "model",
    kind: str = "other",
    bytes: int | None = None,
    name: str | None = None,
    meta: dict[str, Any] | None = None,
    run_id: str | None = None,
    experiment_slug: str | None = None,
) -> Artifact:
    """Register that a run's/experiment's checkpoint/dataset IS already a
    Hugging Face Hub repo — no bytes move, this just records
    `hf://{repo_type}/{repo_id}@{revision}` with `meta.hf_repo_id`/
    `meta.hf_revision`/`meta.hf_repo_type` set (winning over any
    caller-supplied values of the same keys). `bytes` (the expected total
    repo size, if known) makes verify_artifact's check meaningful beyond
    "the revision exists". A real service function, not just an MCP-side
    convenience — see register_git_artifact's docstring."""
    uri = external_refs.build_hf_uri(repo_type, repo_id, revision)
    computed_meta = {
        **(meta or {}),
        "hf_repo_id": repo_id,
        "hf_revision": revision,
        "hf_repo_type": repo_type,
    }
    data = ArtifactCreate(kind=ArtifactKind(kind), uri=uri, bytes=bytes, name=name, meta=computed_meta)
    return await register_artifact(data, run_id=run_id, experiment_slug=experiment_slug)


async def get_artifact(artifact_id: int) -> Artifact:
    pool = await get_pool()
    row = await pool.fetchrow(
        f"SELECT {_ARTIFACT_COLUMNS} FROM artifact WHERE id = $1",
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
    rest/) picks whose GitHub/HF token to use, preferring that user's own
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
        f"""
        UPDATE artifact SET verify_status = $1, verified_at = now(), verify_detail = $2
        WHERE id = $3
        RETURNING {_ARTIFACT_COLUMNS}
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
    q_builder = _QueryBuilder()

    where = ["1=1"]
    if experiment:
        where.append(f"e.slug = {q_builder.bind(experiment)}")
    if model:
        model_param = q_builder.bind(model)
        where.append(f"(r.config->>'model' = {model_param} OR e.design->>'model' = {model_param})")
    if kind:
        where.append(f"a.kind = {q_builder.bind(kind)}")

    limit_param = q_builder.bind(limit)
    rows = await pool.fetch(
        f"""
        SELECT {_artifact_columns("a")}
        FROM artifact a
        JOIN run r ON r.id = a.run_id
        JOIN experiment e ON e.id = r.experiment_id
        WHERE {" AND ".join(where)}
        ORDER BY a.created_at DESC
        LIMIT {limit_param}
        """,
        *q_builder.params,
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
        f"""
        SELECT {_ARTIFACT_COLUMNS}
        FROM artifact
        WHERE name = $1 AND sha256 = $2
        ORDER BY (role = $3) DESC, created_at ASC
        LIMIT 1
        """,
        name,
        sha256,
        ArtifactRole.PRODUCED.value,
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
