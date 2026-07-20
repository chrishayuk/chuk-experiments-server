"""REST surface (spec §4). Each handler: check scope, validate body into a
Pydantic model, call `service`, return the result. All error translation goes
through `errors.error_payload` so REST and MCP report failures the same way.

chuk-mcp-server's endpoint registry keys routes by path string alone (not
path+method), so two `@mcp.endpoint` calls for the same path silently
overwrite each other. Routes that need more than one HTTP method are
therefore registered ONCE with `methods=[...]` and dispatch on
`request.method` inside a single handler.

Split by domain (programmes, experiments, search, queue, runs, artifacts,
pins, users) — each submodule registers its own routes as an import-time
side effect via `@mcp.endpoint`, so importing them here (in any order: no
two routes across different submodules conflict at the same path depth,
confirmed when this was split from a single rest.py) is what makes those
routes live. `cli.py`/tests import this package itself to trigger it.
"""

from . import (
    artifacts as artifacts,
    experiments as experiments,
    pins as pins,
    programmes as programmes,
    queue as queue,
    runs as runs,
    search as search,
    users as users,
)
