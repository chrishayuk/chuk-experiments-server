"""The shared ChukMCPServer instance. `rest.py` and `tools.py` both import
`mcp` from here and decorate it — this module owns none of the route/tool
logic itself, so it stays a stable import target for both surfaces."""

from chuk_mcp_server import ChukMCPServer

from ._version import __version__

mcp = ChukMCPServer(
    name="chuk-experiments",
    version=__version__,
    description="Experiment registry & results server: programmes, experiments, runs, results, artifacts",
)
