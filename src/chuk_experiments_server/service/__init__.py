"""Shared business logic — every REST endpoint and every MCP tool calls into here.

Keeping DB access in one place is what makes "the server never executes
anything, just records" tractable: this package is the only thing that talks
to Postgres. Every public function takes/returns the Pydantic models from
`models.py`, so both entry points (REST body parsing, MCP tool arguments)
validate once, at the edge, in the same shape.

Split by domain (programmes, experiments, runs, results, artifacts, users) —
every external caller (rest/, web.py, cli.py, errors.py) reaches these
through module-qualified `service.<name>` attribute access, so this file
re-exports the full public surface here rather than callers importing from
the individual submodules directly.
"""

from ._shared import ConflictError, NotFoundError, ValidationError
from .artifacts import (
    find_artifact_by_name_sha as find_artifact_by_name_sha,
    find_checkpoints as find_checkpoints,
    get_artifact as get_artifact,
    get_artifact_lineage as get_artifact_lineage,
    get_pin as get_pin,
    list_external_ref_artifacts as list_external_ref_artifacts,
    list_pins as list_pins,
    register_artifact as register_artifact,
    register_git_artifact as register_git_artifact,
    register_hf_artifact as register_hf_artifact,
    set_pin as set_pin,
    verify_artifact as verify_artifact,
)
from .experiments import (
    append_writeup as append_writeup,
    create_experiment as create_experiment,
    get_experiment as get_experiment,
    get_index as get_index,
    get_research_health as get_research_health,
    list_experiments as list_experiments,
    search_experiments as search_experiments,
    update_experiment as update_experiment,
)
from .programmes import get_or_create_programme as get_or_create_programme, list_programmes as list_programmes
from .results import mark_result_superseded as mark_result_superseded, submit_result as submit_result
from .runs import (
    _pack_runs_by_session_budget as _pack_runs_by_session_budget,
    cancel_run as cancel_run,
    claim_queue as claim_queue,
    compare_runs as compare_runs,
    enqueue_run as enqueue_run,
    get_run as get_run,
    peek_queue as peek_queue,
    renew_lease as renew_lease,
    sweep_expired_leases as sweep_expired_leases,
    update_run as update_run,
)
from .users import (
    clear_user_token as clear_user_token,
    create_api_key as create_api_key,
    create_user as create_user,
    get_active_user_by_email as get_active_user_by_email,
    get_user_token as get_user_token,
    get_user_token_status as get_user_token_status,
    list_api_keys as list_api_keys,
    list_team_users as list_team_users,
    revoke_api_key as revoke_api_key,
    revoke_user as revoke_user,
    set_user_token as set_user_token,
)

__all__ = [
    "ConflictError",
    "NotFoundError",
    "ValidationError",
    "append_writeup",
    "cancel_run",
    "claim_queue",
    "clear_user_token",
    "compare_runs",
    "create_api_key",
    "create_experiment",
    "create_user",
    "enqueue_run",
    "find_artifact_by_name_sha",
    "find_checkpoints",
    "get_active_user_by_email",
    "get_artifact",
    "get_artifact_lineage",
    "get_experiment",
    "get_index",
    "get_or_create_programme",
    "get_pin",
    "get_research_health",
    "get_run",
    "get_user_token",
    "get_user_token_status",
    "list_api_keys",
    "list_experiments",
    "list_external_ref_artifacts",
    "list_pins",
    "list_programmes",
    "list_team_users",
    "mark_result_superseded",
    "peek_queue",
    "register_artifact",
    "register_git_artifact",
    "register_hf_artifact",
    "renew_lease",
    "revoke_api_key",
    "revoke_user",
    "search_experiments",
    "set_pin",
    "set_user_token",
    "submit_result",
    "sweep_expired_leases",
    "update_experiment",
    "update_run",
    "verify_artifact",
]
