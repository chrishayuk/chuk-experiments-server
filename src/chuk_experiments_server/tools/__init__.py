"""MCP surface (spec §5). Every tool forwards to this server's own REST API
(see internal_client.py) using the *calling agent's own bearer token* —
extracted from the ambient MCP context via auth.bearer_from_mcp_context() —
so the REST layer performs the exact same scope check it would for any
other client. tools/ holds no auth/validation logic of its own; it's a
thin MCP-to-REST adapter, one level further out than "the MCP server is a
thin layer over the same service functions" (the original spec's phrasing)
— now it's a thin layer over the same REST API instead, so the UI, MCP
agents, and any external REST client all go through one code path.

A tool never raises on a failed request — it returns whatever JSON body the
REST endpoint produced (its own error shape included), so a failed lookup
reads as data to the calling agent rather than an opaque tool-call failure.

Split by domain (programmes, experiments, runs, queue, results, artifacts,
pins), matching service/ and rest/'s split. `@mcp.tool` registers by name,
not by any path-matching order, so unlike rest/ there's no import-order
constraint between submodules — this __init__ re-exports the full public
surface so every external caller (tests, cli.py) keeps working via
`tools.<name>` attribute access.
"""

from .artifacts import (
    find_checkpoints as find_checkpoints,
    get_artifact_lineage as get_artifact_lineage,
    list_external_ref_artifacts as list_external_ref_artifacts,
    register_artifact as register_artifact,
    register_git_artifact as register_git_artifact,
    register_hf_artifact as register_hf_artifact,
    upload_artifact_to_drive as upload_artifact_to_drive,
    upload_artifacts_batch as upload_artifacts_batch,
    verify_artifact as verify_artifact,
)
from .experiments import (
    append_writeup as append_writeup,
    create_experiment as create_experiment,
    get_experiment as get_experiment,
    get_index as get_index,
    list_experiments as list_experiments,
    record_experiment_conclusion as record_experiment_conclusion,
    search_experiments as search_experiments,
    update_experiment_status as update_experiment_status,
)
from .pins import get_pin as get_pin, list_pins as list_pins, set_pin as set_pin
from .programmes import list_programmes as list_programmes
from .queue import peek_queue as peek_queue
from .results import mark_result_superseded as mark_result_superseded, submit_result as submit_result
from .runs import (
    cancel_run as cancel_run,
    compare_runs as compare_runs,
    enqueue_run as enqueue_run,
    get_run as get_run,
    set_run_status as set_run_status,
)
