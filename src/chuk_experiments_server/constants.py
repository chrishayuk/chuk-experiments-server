"""Every enum and named constant used across the service. Nothing below should
be re-typed as a bare string/int literal anywhere else in the codebase."""

from enum import Enum


class Scope(str, Enum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class ExperimentStatus(str, Enum):
    DRAFT = "draft"
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


class RunStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    LOST = "lost"
    CANCELLED = "cancelled"


#: Statuses a run can be cancelled from — see service.cancel_run.
CANCELLABLE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.CLAIMED)
#: Statuses a lease can be renewed on — see service.renew_lease.
LEASABLE_RUN_STATUSES = (RunStatus.CLAIMED, RunStatus.RUNNING)


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NA = "n/a"


class ArtifactKind(str, Enum):
    CHECKPOINT = "checkpoint"
    LOG = "log"
    DATASET = "dataset"
    FIGURE = "figure"
    TENSOR = "tensor"
    OTHER = "other"


class MetricOp(str, Enum):
    """search_experiments' metric predicate operator — a closed whitelist so
    the SQL comparison operator is never built from raw user input."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


METRIC_OP_SQL: dict[MetricOp, str] = {
    MetricOp.EQ: "=",
    MetricOp.NE: "!=",
    MetricOp.GT: ">",
    MetricOp.GTE: ">=",
    MetricOp.LT: "<",
    MetricOp.LTE: "<=",
}


# --- Pagination -------------------------------------------------------------

DEFAULT_LIST_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 20
MAX_LIST_LIMIT = 500

# --- Queue -------------------------------------------------------------

#: Lease duration granted by a claim/renewal when the caller doesn't specify one.
DEFAULT_LEASE_SECONDS = 600
#: Number of times a run may be requeued after an expired lease before it's marked 'lost'.
DEFAULT_MAX_CLAIM_ATTEMPTS = 3

# --- DB pool ------------------------------------------------------------

DB_POOL_MIN_SIZE = 1
DB_POOL_MAX_SIZE = 10

# --- Auth ------------------------------------------------------------------

BEARER_PREFIX = "bearer"
AUTHORIZATION_HEADER = "authorization"

# --- Server ------------------------------------------------------------------

DEFAULT_HTTP_HOST = "0.0.0.0"  # noqa: S104 - intentional bind-all for containers
DEFAULT_HTTP_PORT = 8000

# --- R2 / object storage (spec §9) -------------------------------------------

#: "Presigned URLs are short-lived (15 min PUT, 1 h GET)" — spec §9.
PRESIGN_PUT_EXPIRY_SECONDS = 900
PRESIGN_GET_EXPIRY_SECONDS = 3600
R2_SIGNATURE_VERSION = "s3v4"
R2_REGION = "auto"
