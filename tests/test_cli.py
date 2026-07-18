"""cli.py tests. `main()`'s dispatch is tested by stubbing the underlying
`_migrate`/`_serve`/`_create_key`/`_sweep` functions (so a test doesn't
actually bind a socket or otherwise run for real); each of those functions
also gets its own direct test against the real test database."""

import pytest

from chuk_experiments_server import auth, cli
from chuk_experiments_server.db import close_pool


# --- _build_parser -----------------------------------------------------------


def test_build_parser_migrate():
    args = cli._build_parser().parse_args(["migrate"])
    assert args.command == "migrate"


def test_build_parser_serve_defaults():
    args = cli._build_parser().parse_args(["serve"])
    assert args.command == "serve"
    assert args.host
    assert args.port


def test_build_parser_serve_overrides():
    args = cli._build_parser().parse_args(["serve", "--host", "0.0.0.0", "--port", "1234"])
    assert (args.host, args.port) == ("0.0.0.0", 1234)


def test_build_parser_keys_create():
    args = cli._build_parser().parse_args(
        ["keys", "create", "harness", "--scope", "read", "--scope", "write"]
    )
    assert (args.command, args.keys_command, args.name, args.scopes) == (
        "keys",
        "create",
        "harness",
        ["read", "write"],
    )


def test_build_parser_sweep_defaults():
    args = cli._build_parser().parse_args(["sweep"])
    assert args.command == "sweep"
    assert args.max_attempts


def test_build_parser_requires_a_command():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args([])


# --- main() dispatch ---------------------------------------------------------


def test_main_dispatches_to_migrate(monkeypatch):
    calls = []

    async def fake_migrate():
        calls.append(True)

    monkeypatch.setattr(cli, "_migrate", fake_migrate)
    monkeypatch.setattr("sys.argv", ["chuk-experiments-server", "migrate"])
    cli.main()
    assert calls == [True]


def test_main_dispatches_to_serve(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_serve", lambda host, port, log_level: calls.append((host, port, log_level)))
    monkeypatch.setattr(
        "sys.argv", ["chuk-experiments-server", "serve", "--host", "0.0.0.0", "--port", "1234"]
    )
    cli.main()
    assert calls == [("0.0.0.0", 1234, "INFO")]


def test_main_dispatches_to_keys_create(monkeypatch):
    calls = []

    async def fake_create_key(name, scopes):
        calls.append((name, scopes))

    monkeypatch.setattr(cli, "_create_key", fake_create_key)
    monkeypatch.setattr(
        "sys.argv",
        ["chuk-experiments-server", "keys", "create", "harness", "--scope", "read", "--scope", "write"],
    )
    cli.main()
    assert calls == [("harness", ["read", "write"])]


def test_main_dispatches_to_sweep(monkeypatch):
    calls = []

    async def fake_sweep(max_attempts):
        calls.append(max_attempts)

    monkeypatch.setattr(cli, "_sweep", fake_sweep)
    monkeypatch.setattr("sys.argv", ["chuk-experiments-server", "sweep", "--max-attempts", "5"])
    cli.main()
    assert calls == [5]


def test_main_exits_for_unrecognized_command(monkeypatch):
    class _FakeArgs:
        command = "bogus"
        log_level = "INFO"

    class _FakeParser:
        def parse_args(self):
            return _FakeArgs()

    monkeypatch.setattr(cli, "_build_parser", lambda: _FakeParser())
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


# --- Underlying async implementations, against the real test database -------


async def test_migrate_applies_schema_and_upserts_bootstrap_key(_apply_schema, monkeypatch):
    monkeypatch.setenv("EXPERIMENTS_BOOTSTRAP_KEY", "clitest:read|write:cli-test-raw-key")
    await cli._migrate()
    try:
        record = await auth.authenticate("cli-test-raw-key")
        assert record is not None
        assert record.name == "clitest"
    finally:
        await close_pool()


async def test_migrate_without_bootstrap_key_is_a_noop(_apply_schema, monkeypatch):
    monkeypatch.delenv("EXPERIMENTS_BOOTSTRAP_KEY", raising=False)
    await cli._migrate()  # must not raise
    await close_pool()


async def test_migrate_upserts_bootstrap_admin_user(_apply_schema, monkeypatch):
    from chuk_experiments_server import service
    from chuk_experiments_server.constants import Scope

    monkeypatch.setenv("DASHBOARD_ALLOWED_EMAIL", "cli-admin@example.com")
    await cli._migrate()
    try:
        user = await service.get_active_user_by_email("cli-admin@example.com")
        assert user is not None
        assert user.role == Scope.ADMIN
    finally:
        await close_pool()


async def test_migrate_without_dashboard_allowed_email_skips_user_bootstrap(_apply_schema, monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOWED_EMAIL", raising=False)
    await cli._migrate()  # must not raise
    await close_pool()


async def test_create_key_prints_raw_key_once(_apply_schema, capsys):
    await cli._create_key("harness-colab", ["read", "write"])
    try:
        lines = capsys.readouterr().out.splitlines()
        raw_key = lines[1]
        record = await auth.authenticate(raw_key)
        assert record is not None
        assert record.name == "harness-colab"
    finally:
        await close_pool()


async def test_sweep_reports_requeued_and_lost_counts(_apply_schema, capsys):
    await cli._sweep(3)
    out = capsys.readouterr().out
    assert "Requeued" in out
    await close_pool()


def test_register_rest_routes_imports_rest_and_web():
    cli._register_rest_routes()  # must not raise; modules are cached after first import
