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
    author: str
    created_at: datetime


class WriteupCreate(BaseModel):
    body_md: str


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


class RunSummary(RecordModel):
    id: int
    slug: str
    status: RunStatus
    backend: str | None = None
    wandb_url: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cost_usd: float | None = None


class ExperimentSummary(RecordModel):
    id: int
    slug: str
    title: str
    status: ExperimentStatus
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    programme_slug: str
    programme_name: str


class Experiment(RecordModel):
    id: int
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
    slug: str
    title: str
    hypothesis: str | None = None
    design: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    status: ExperimentStatus = ExperimentStatus.PLANNED


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
    run_id: int
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
    run_id: int
    kind: ArtifactKind
    uri: str
    bytes: int | None = None
    sha256: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ArtifactCreate(BaseModel):
    kind: ArtifactKind
    uri: str
    bytes: int | None = None
    sha256: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Run(RecordModel):
    id: int
    slug: str
    status: RunStatus
    priority: int = 0
    depends_on: list[int] = Field(default_factory=list)
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
    slug: str
    backend: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    budget_seconds: int | None = None
    status: RunStatus = RunStatus.QUEUED
    priority: int = 0
    depends_on: list[int] = Field(default_factory=list)
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
    run_id: int
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

    def has_scope(self, scope: Scope) -> bool:
        return scope in self.scopes or Scope.ADMIN in self.scopes
