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

#: Dashboard status-pill color class ("good"/"run"/"warn"/"bad"/"mut") —
#: matches the shared gpu-training-harness dashboard's status-color
#: convention, so the two sibling dashboards read consistently.
STATUS_CSS_CLASS: dict[str, str] = {
    ExperimentStatus.DRAFT.value: "mut",
    ExperimentStatus.PLANNED.value: "mut",
    ExperimentStatus.RUNNING.value: "run",
    ExperimentStatus.COMPLETED.value: "good",
    ExperimentStatus.ABANDONED.value: "bad",
    ExperimentStatus.SUPERSEDED.value: "mut",
    RunStatus.QUEUED.value: "mut",
    RunStatus.CLAIMED.value: "run",
    RunStatus.FAILED.value: "bad",
    RunStatus.KILLED.value: "bad",
    RunStatus.LOST.value: "bad",
    RunStatus.CANCELLED.value: "mut",
}


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


class ArtifactRole(str, Enum):
    """Lineage signal on an artifact row: did this run make it, or is this
    run just referencing content an earlier run already produced (a dedup
    hit — see service.find_artifact_by_name_sha)?"""

    PRODUCED = "produced"
    USED = "used"


class TokenProvider(str, Enum):
    """Which external service a per-user token (app_user.*_token_encrypted)
    is for — see token_crypto.py and service.set_user_token/get_user_token."""

    GITHUB = "github"
    HUGGINGFACE = "huggingface"


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

#: list_experiments' sort param, allow-listed against real SQL column
#: expressions — never interpolate a caller-supplied column name directly,
#: same defensive pattern as METRIC_OP_SQL for search's metric operator.
EXPERIMENT_SORT_COLUMNS: dict[str, str] = {
    "title": "e.title",
    "status": "e.status",
    "created_at": "e.created_at",
    "updated_at": "e.updated_at",
    "programme_slug": "p.slug",
}
DEFAULT_EXPERIMENT_SORT = "updated_at"
DEFAULT_EXPERIMENT_ORDER = "desc"

# --- Queue -------------------------------------------------------------

#: Lease duration granted by a claim/renewal when the caller doesn't specify one.
DEFAULT_LEASE_SECONDS = 600
#: Number of times a run may be requeued after an expired lease before it's marked 'lost'.
DEFAULT_MAX_CLAIM_ATTEMPTS = 3

# --- DB pool ------------------------------------------------------------

DB_POOL_MIN_SIZE = 1
DB_POOL_MAX_SIZE = 10

# --- Sortable ids (experiment.id, run.id, and their auto-generated slugs) ----
# Format: {PREFIX}-{YYYYMMDD}-{HHMMSS}-{5-digit zero-padded sequence number},
# e.g. "RUN-20260718-160217-00397" — matches the convention already used by
# the gpu-training-harness train server. Sorts chronologically as a plain
# string, unlike a UUID or an opaque serial int.

EXPERIMENT_ID_PREFIX = "EXP"
RUN_ID_PREFIX = "RUN"
ID_SEQUENCE_PAD_WIDTH = 5
EXPERIMENT_REF_SEQUENCE = "experiment_ref_seq"
RUN_REF_SEQUENCE = "run_ref_seq"

# --- Auth ------------------------------------------------------------------

BEARER_PREFIX = "bearer"
AUTHORIZATION_HEADER = "authorization"

#: A dashboard user's `role` reuses the Scope vocabulary (read/write/admin —
#: the values already line up exactly) rather than a parallel enum. Ceiling
#: on what scopes a self-service-minted API key may carry, keyed by the
#: creating user's role — e.g. a "write"-role user can mint read+write keys
#: for their own tools, but never an admin-scoped one.
ROLE_SCOPE_CEILING: dict[Scope, frozenset[Scope]] = {
    Scope.READ: frozenset({Scope.READ}),
    Scope.WRITE: frozenset({Scope.READ, Scope.WRITE}),
    Scope.ADMIN: frozenset({Scope.READ, Scope.WRITE, Scope.ADMIN}),
}

#: Ordinal ranking so `require_dashboard_role` can check "is this user's role
#: at least as privileged as the route requires" with a plain comparison.
ROLE_ORDER: dict[Scope, int] = {Scope.READ: 0, Scope.WRITE: 1, Scope.ADMIN: 2}

# --- Server ------------------------------------------------------------------

DEFAULT_HTTP_HOST = "0.0.0.0"  # noqa: S104 - intentional bind-all for containers
DEFAULT_HTTP_PORT = 8000

# --- R2 / object storage (spec §9) -------------------------------------------

#: "Presigned URLs are short-lived (15 min PUT, 1 h GET)" — spec §9.
PRESIGN_PUT_EXPIRY_SECONDS = 900
PRESIGN_GET_EXPIRY_SECONDS = 3600
R2_SIGNATURE_VERSION = "s3v4"
R2_REGION = "auto"

#: Artifact URIs pointing at a Drive folder archived by
#: scripts/archive_*_to_drive.py — pointer/scheme-agnostic alongside R2's
#: s3:// scheme, per ROADMAP.md's Google Drive archival phase.
GDRIVE_URI_PREFIX = "gdrive://"

#: Artifact URIs referencing a git commit or a Hugging Face Hub repo,
#: instead of bytes uploaded through this server — see external_refs.py.
#: `git+https://github.com/{owner}/{repo}@{commit}` (pip's VCS-URL
#: convention) and `hf://model/{repo_id}@{revision}` /
#: `hf://dataset/{repo_id}@{revision}`.
GIT_URI_PREFIXES = ("git+https://", "git+http://", "git+ssh://")
HF_URI_PREFIX = "hf://"

#: register_artifact only accepts a uri that's already reachable in real
#: storage — never a local file:// path or bare filesystem path, which is
#: meaningless to anyone but the exact machine that registered it (and
#: un-downloadable through /v1/artifacts/{id}/download). Local bytes go
#: through upload_artifact_to_drive (small files) or the R2 presign flow
#: (large checkpoints) instead — both hand back one of these prefixes.
VALID_ARTIFACT_URI_PREFIXES = (
    "s3://",
    GDRIVE_URI_PREFIX,
    "https://",
    "http://",
    *GIT_URI_PREFIXES,
    HF_URI_PREFIX,
)

#: register_artifact accepts arbitrary caller-supplied `meta` (e.g. linking
#: a checkpoint that already lives in another project's bucket, with that
#: project's own metadata shape) — but artifact_download follows
#: meta["drive_url"] with an unconditional redirect, so that one specific
#: field must be checked against Drive's real domain before ever being
#: used as a redirect target, regardless of who wrote it.
TRUSTED_DRIVE_URL_PREFIX = "https://drive.google.com/"

# --- Dashboard auth (spec §8/§9 "website behind ... the read key") -----------
# "Sign in with Google", restricted to one email — a browser session, not the
# bearer-token API auth in auth.py (that's for REST/MCP clients, which can
# set an Authorization header; a browser navigating between pages can't).

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_OAUTH_SCOPE = "openid email profile"

SESSION_COOKIE_NAME = "chuk_experiments_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 days
OAUTH_STATE_COOKIE_NAME = "chuk_experiments_oauth_state"
OAUTH_STATE_COOKIE_MAX_AGE_SECONDS = 600  # just needs to survive the redirect round-trip

# --- Markdown rendering (write-up bodies) ------------------------------------

#: Allowlist for sanitizing rendered write-up HTML — DB content isn't
#: necessarily human-authored (agents can append_writeup too), so this is a
#: real trust boundary, not just formatting.
MARKDOWN_ALLOWED_TAGS = [
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "img",
]
MARKDOWN_ALLOWED_ATTRIBUTES = {"a": ["href", "title"], "img": ["src", "alt", "title"]}
