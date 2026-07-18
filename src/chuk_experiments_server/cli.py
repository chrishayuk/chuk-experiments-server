"""Command-line entrypoint: `chuk-experiments-server {migrate|serve|keys create}`.

HTTP-only by design — this server is a shared multi-agent record store meant
to run on Fly.io, not a per-user stdio server for Claude Desktop. Bearer-token
auth (see auth.py) needs real HTTP headers to read from, which stdio mode
doesn't have.
"""

import argparse
import asyncio
import logging
import os
import sys

from . import auth, service
from ._version import __version__
from .config import settings
from .constants import DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT, DEFAULT_MAX_CLAIM_ATTEMPTS
from .db import apply_migrations, close_pool

logger = logging.getLogger(__name__)


async def _migrate() -> None:
    await apply_migrations()
    if settings.bootstrap_key:
        await auth.upsert_bootstrap_key(settings.bootstrap_key)
        logger.info("Bootstrap API key upserted")
    await close_pool()


def _register_rest_routes() -> None:
    """chuk-mcp-server's HTTPServer.__init__ clears the endpoint registry and
    registers its own built-ins (ping/health/mcp/...) BEFORE calling
    `post_register_hook` — see chuk_mcp_server/http_server.py's
    `_register_endpoints`. Importing `rest`/`web` (whose @mcp.endpoint
    decorators fire at import time) has to happen inside that hook, not
    before `mcp.run()`, or our routes get wiped by that clear."""
    from . import rest, web  # noqa: F401 - imported for their @mcp.endpoint side effects


def _serve(host: str, port: int, log_level: str) -> None:
    # `tools` is imported here (not at module scope) purely so `migrate`/`keys`
    # subcommands don't pay for registering every tool before they run — MCP
    # tools aren't affected by the endpoint-registry clear described above.
    from . import tools  # noqa: F401 - imported for its @mcp.tool side effects
    from .server import mcp

    # tools.py/web.py call this server's own REST API over HTTP (see
    # internal_client.py) — point that loopback client at whatever port
    # we're actually about to bind, rather than a hardcoded default.
    os.environ.setdefault("INTERNAL_API_BASE_URL", f"http://127.0.0.1:{port}")

    mcp.run(host=host, port=port, log_level=log_level.lower(), post_register_hook=_register_rest_routes)


async def _create_key(name: str, scopes: list[str]) -> None:
    raw = auth.generate_key()
    spec = f"{name}:{'|'.join(scopes)}:{raw}"
    await auth.upsert_bootstrap_key(spec)
    await close_pool()
    print(f"Created API key '{name}' with scopes {scopes}:")
    print(raw)
    print("\nThis is the only time the raw key is shown — only its hash is stored.")


async def _sweep(max_attempts: int) -> None:
    """Requeue/expire runs whose claim lease lapsed. Meant to run on a
    schedule (e.g. a Fly.io scheduled machine, or any cron hitting
    `POST /v1/queue/sweep` with an admin key) — see service.sweep_expired_leases."""
    result = await service.sweep_expired_leases(max_attempts)
    await close_pool()
    print(f"Requeued {result.requeued}, marked lost {result.lost}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chuk-experiments-server", description="Experiment registry & results server"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate", help="Apply the Postgres schema and upsert the bootstrap API key")

    serve_parser = subparsers.add_parser("serve", help="Run the HTTP server (REST + MCP)")
    serve_parser.add_argument("--host", default=DEFAULT_HTTP_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)

    keys_parser = subparsers.add_parser("keys", help="Manage API keys")
    keys_subparsers = keys_parser.add_subparsers(dest="keys_command", required=True)
    create_parser = keys_subparsers.add_parser("create", help="Create a new API key")
    create_parser.add_argument("name", help="Human-readable key name (e.g. 'harness-colab')")
    create_parser.add_argument(
        "--scope",
        dest="scopes",
        action="append",
        required=True,
        choices=["read", "write", "admin"],
        help="Repeatable — e.g. --scope read --scope write",
    )

    sweep_parser = subparsers.add_parser("sweep", help="Requeue/expire runs with a lapsed claim lease")
    sweep_parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_CLAIM_ATTEMPTS)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level), format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    if args.command == "migrate":
        asyncio.run(_migrate())
    elif args.command == "serve":
        _serve(args.host, args.port, args.log_level)
    elif args.command == "keys" and args.keys_command == "create":
        asyncio.run(_create_key(args.name, args.scopes))
    elif args.command == "sweep":
        asyncio.run(_sweep(args.max_attempts))
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
