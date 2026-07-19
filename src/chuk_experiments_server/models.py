"""Pydantic models. These are the single source of truth for shape + validation —
REST endpoints parse request bodies into the `*Create`/`*Update` models below,
MCP tools build the same models from their keyword arguments, and `service.py`
returns the entity models to both callers. Nothing downstream re-validates."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .constants import (
    DEFAULT_LEASE_SECONDS,
    ArtifactKind,
    ArtifactRole,
    ExperimentStatus,
    MetricOp,
    RunStatus,
    Scope,
    Verdict,
)


class RecordModel(BaseModel):
    """Base for models hydrated straight from an asyncpg Record."""

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Programme
# ---------------------------------------------------------------------------


class Programme(RecordModel):
    id: int
    slug: str
    name: str
    description: str | None = None
    created_at: datetime
    experiment_count: int | None = None


class ProgrammeCreate(BaseModel):
    slug: str
    name: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Writeup
# ---------------------------------------------------------------------------


class Writeup(RecordModel):
    version: int
    body_md: str
    #: Sanitized HTML rendering of body_md (markdown_render.render), computed
    #: at read time — never stored — so any REST/MCP consumer gets it
    #: without needing its own markdown parser (the dashboard SPA in
    #: particular; see web.py).
    body_html: str
    author: str
    created_at: datetime


class WriteupCreate(BaseModel):
    body_md: str


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


class RunSummary(RecordModel):
    id: str
    slug: str
    status: RunStatus
    backend: str | None = None
    wandb_url: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cost_usd: float | None = None


class ExperimentSummary(RecordModel):
    id: str
    slug: str
    title: str
    status: ExperimentStatus
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    programme_slug: str
    programme_name: str


class Experiment(RecordModel):
    id: str
    slug: str
    title: str
    status: ExperimentStatus
    hypothesis: str | None = None
    design: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    programme_slug: str
    programme_name: str
    latest_writeup: Writeup | None = None
    runs: list[RunSummary] = Field(default_factory=list)


class ExperimentCreate(BaseModel):
    programme: str
    # Auto-generated (EXP-YYYYMMDD-HHMMSS-<rand>) via service._generate_ref
    # when omitted — human-chosen slugs still win when supplied.
    slug: str | None = None
    title: str
    hypothesis: str | None = None
    design: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    status: ExperimentStatus = ExperimentStatus.PLANNED
    # Display name for the programme, used only the first time `programme` is
    # seen — get_or_create_programme humanizes the slug otherwise, which is
    # fine for "state-construction" -> "State Construction" but wrong for
    # acronyms like "larql" -> "Larql".
    programme_name: str | None = None


class ExperimentUpdate(BaseModel):
    status: ExperimentStatus | None = None
    tags: list[str] | None = None


class SearchHit(RecordModel):
    slug: str
    title: str
    status: ExperimentStatus
    programme_slug: str
    rank: float
    snippet: str


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class Result(RecordModel):
    id: int
    run_id: str
    name: str
    value: float | None = None
    value_json: dict[str, Any] | None = None
    verdict: Verdict | None = None
    notes: str | None = None
    submitted_by: str
    created_at: datetime


class ResultCreate(BaseModel):
    name: str
    value: float | None = None
    value_json: dict[str, Any] | None = None
    verdict: Verdict | None = None
    notes: str | None = None


class Artifact(RecordModel):
    id: int
    run_id: str
    kind: ArtifactKind
    uri: str
    bytes: int | None = None
    sha256: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    name: str | None = None
    role: ArtifactRole = ArtifactRole.PRODUCED
    #: Set only by service.verify_artifact (external_refs.py) for git+/hf://
    #: reference artifacts — never caller-settable via ArtifactCreate.
    verify_status: str | None = None
    verified_at: datetime | None = None
    verify_detail: str | None = None


class ArtifactCreate(BaseModel):
    kind: ArtifactKind
    uri: str
    bytes: int | None = None
    sha256: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    #: Optional — enables dedup lookups (find_artifact_by_name_sha) and
    #: lineage grouping. A one-off pointer registration (e.g. linking a
    #: checkpoint that already lives in another project's bucket) doesn't
    #: need one.
    name: str | None = None
    role: ArtifactRole = ArtifactRole.PRODUCED


class ArtifactPresignRequest(BaseModel):
    filename: str
    kind: ArtifactKind = ArtifactKind.OTHER
    content_type: str | None = None


class ArtifactUploadRequest(BaseModel):
    """Body for POST .../artifacts/upload — content travels through this
    server (base64, in the JSON body) to Google Drive, unlike the R2
    presign flow where bytes never transit this server at all. Intended
    for small provenance/config/log/dataset files, not large checkpoints.

    name is required (not optional, unlike plain ArtifactCreate) — it's
    the whole point of this endpoint: content-addressed dedup keyed on
    (name, sha256) of the uploaded bytes, so the same harness/dataset
    reused across many runs is uploaded to Drive only once."""

    filename: str
    kind: ArtifactKind = ArtifactKind.OTHER
    content_base64: str
    name: str
    #: None = auto-infer (PRODUCED on first upload of this name+sha256,
    #: USED when it's a dedup hit against an earlier run's upload).
    role: ArtifactRole | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ArtifactBatchUploadRequest(BaseModel):
    """Body for POST .../artifacts/upload-batch — the same content-addressed
    upload as ArtifactUploadRequest, but N files in one round trip instead of
    N separate MCP tool calls. Each item dedups independently, including
    against another item earlier in the same batch."""

    items: list[ArtifactUploadRequest] = Field(min_length=1)


class ArtifactPresignResponse(BaseModel):
    upload_url: str
    uri: str
    expires_in: int


class ArtifactLineage(BaseModel):
    """Which run produced this artifact's content, and which other runs
    have since referenced the same (name, sha256) — falls out of grouping
    existing artifact rows by role, no separate lineage table needed."""

    produced_by_run_id: str | None = None
    used_by_run_ids: list[str] = Field(default_factory=list)


class ArtifactPin(RecordModel):
    """A named, repointable alias to a specific artifact — e.g.
    'tok-v12-tokenizer:latest' — W&B-style."""

    id: int
    name: str
    artifact_id: int
    updated_at: datetime


class PinSummary(RecordModel):
    """list_pins' shape — an ArtifactPin denormalized with just enough of
    its target artifact (run, kind, uri, the artifact's own name) to render
    a pins list without a second request per row."""

    id: int
    name: str
    artifact_id: int
    updated_at: datetime
    run_id: str
    kind: ArtifactKind
    uri: str
    artifact_name: str | None = None


class ExternalRefSummary(RecordModel):
    """list_external_ref_artifacts' shape — a git+/hf:// artifact
    denormalized with enough of its owning run/experiment to render a
    dashboard-wide browse screen without a second request per row."""

    id: int
    run_id: str
    experiment_slug: str
    experiment_title: str
    kind: ArtifactKind
    uri: str
    name: str | None = None
    role: ArtifactRole
    meta: dict[str, Any] = Field(default_factory=dict)
    verify_status: str | None = None
    verified_at: datetime | None = None
    verify_detail: str | None = None
    created_at: datetime


class ArtifactPinSet(BaseModel):
    artifact_id: int


class Run(RecordModel):
    id: str
    slug: str
    status: RunStatus
    priority: int = 0
    depends_on: list[str] = Field(default_factory=list)
    workspec: dict[str, Any] = Field(default_factory=dict)
    requirements: dict[str, Any] = Field(default_factory=dict)
    est_seconds: int | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    claim_attempts: int = 0
    backend: str | None = None
    harness_session_id: str | None = None
    wandb_url: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    budget_seconds: int | None = None
    cost_usd: float | None = None
    created_at: datetime
    experiment_slug: str
    experiment_title: str
    results: list[Result] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)


class RunCreate(BaseModel):
    """Enqueues a run — see spec §6a. `workspec` should be everything a
    harness worker needs to execute the run with no other context."""

    experiment: str
    # Auto-generated (RUN-YYYYMMDD-HHMMSS-<rand>) via service._generate_ref
    # when omitted — an explicit slug still wins when supplied.
    slug: str | None = None
    backend: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    budget_seconds: int | None = None
    status: RunStatus = RunStatus.QUEUED
    priority: int = 0
    depends_on: list[str] = Field(default_factory=list)
    workspec: dict[str, Any] = Field(default_factory=dict)
    requirements: dict[str, Any] = Field(default_factory=dict)
    est_seconds: int | None = None


class RunUpdate(BaseModel):
    status: RunStatus | None = None
    wandb_url: str | None = None
    harness_session_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cost_usd: float | None = None


class RunComparisonRow(RecordModel):
    run_id: str
    run_slug: str
    experiment_slug: str
    value: float | None = None
    value_json: dict[str, Any] | None = None
    verdict: Verdict | None = None


# ---------------------------------------------------------------------------
# Queue (spec §6a)
# ---------------------------------------------------------------------------


class QueueClaimRequest(BaseModel):
    backend: str
    session_seconds: int
    lease_seconds: int = DEFAULT_LEASE_SECONDS


class LeaseRenewal(BaseModel):
    lease_seconds: int = DEFAULT_LEASE_SECONDS


class QueueSweepResult(BaseModel):
    requeued: int
    lost: int


# ---------------------------------------------------------------------------
# Search (spec §5a)
# ---------------------------------------------------------------------------


class MetricPredicate(BaseModel):
    name: str
    op: MetricOp
    value: float


class SearchFilters(BaseModel):
    programme: str | None = None
    status: ExperimentStatus | None = None
    tags: list[str] | None = None
    config_key: str | None = None
    config_value: str | None = None
    metric: MetricPredicate | None = None


class HeadlineMetric(BaseModel):
    name: str
    value: float | None = None
    verdict: Verdict | None = None


class IndexEntry(RecordModel):
    """One row of `get_index()` — spec §5a: the whole catalogue is small
    enough that an agent reads it in one call and matches semantically
    in-context, rather than relying on FTS alone."""

    slug: str
    title: str
    status: ExperimentStatus
    programme_slug: str
    tags: list[str] = Field(default_factory=list)
    hypothesis: str | None = None
    headline_metric: HeadlineMetric | None = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class ApiKey(RecordModel):
    id: int
    name: str
    scopes: list[Scope]
    #: NULL for CLI/bootstrap-created keys with no human user behind them
    #: (e.g. dev-local-key) — see service.verify_artifact's token-resolution
    #: fallback for what happens when this is None.
    created_by_user_id: int | None = None

    def has_scope(self, scope: Scope) -> bool:
        return scope in self.scopes or Scope.ADMIN in self.scopes


# ---------------------------------------------------------------------------
# Users & self-service API keys (dashboard, spec §8 teams/roles)
# ---------------------------------------------------------------------------


class AppUser(RecordModel):
    id: int
    email: str
    role: Scope
    created_at: datetime
    revoked_at: datetime | None = None


class AppUserCreate(BaseModel):
    email: str
    role: Scope


class ApiKeySummary(RecordModel):
    """A key's metadata for the management screen — never the raw value or
    key_hash. `created_by_email` is None for CLI/bootstrap-created keys with
    no human user behind them."""

    id: int
    name: str
    scopes: list[Scope]
    created_at: datetime
    revoked_at: datetime | None = None
    created_by_email: str | None = None


class ApiKeyCreate(BaseModel):
    name: str
    scopes: list[Scope]


class ApiKeyCreateResponse(RecordModel):
    """Same "shown once" contract as the CLI's `keys create` — raw_key is
    never persisted or returned again after this response."""

    id: int
    name: str
    scopes: list[Scope]
    created_at: datetime
    raw_key: str


class UserTokenSet(BaseModel):
    """Body for PUT /v1/me/tokens/{provider} — never round-tripped back
    (see service.get_user_token_status, which only ever reports set/not-set
    booleans, never the value)."""

    token: str


class DashboardIdentity(BaseModel):
    """Who's making a user/key-management request — either a real signed-in
    AppUser, or None standing in for a bearer-ADMIN "system operator" (no
    specific user, matching CLI-created keys' created_by_user_id=NULL)."""

    email: str | None
    role: Scope
    user_id: int | None = None
