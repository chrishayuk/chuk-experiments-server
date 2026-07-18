import pytest

from chuk_experiments_server.config import Settings
from chuk_experiments_server.constants import DEFAULT_HTTP_PORT


@pytest.fixture
def blank_settings(monkeypatch):
    """A fresh Settings instance with every relevant env var cleared, so
    each test controls exactly what's set rather than inheriting the real
    project .env or the test-session dashboard-auth defaults."""
    for key in (
        "DATABASE_URL",
        "EXPERIMENTS_BOOTSTRAP_KEY",
        "MCP_LOG_LEVEL",
        "R2_BUCKET",
        "R2_ENDPOINT_URL",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "GOOGLE_DRIVE_CLIENT_ID",
        "GOOGLE_DRIVE_CLIENT_SECRET",
        "GOOGLE_DRIVE_REFRESH_TOKEN",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
        "DASHBOARD_ALLOWED_EMAIL",
        "SESSION_SECRET",
        "INTERNAL_API_BASE_URL",
        "INTERNAL_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    return Settings()


def test_database_url_missing_raises(blank_settings):
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        blank_settings.database_url


def test_database_url_present(blank_settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert blank_settings.database_url == "postgresql://x"


def test_bootstrap_key_defaults_to_none(blank_settings):
    assert blank_settings.bootstrap_key is None


def test_log_level_defaults_to_info(blank_settings):
    assert blank_settings.log_level == "INFO"


def test_log_level_overridden(blank_settings, monkeypatch):
    monkeypatch.setenv("MCP_LOG_LEVEL", "DEBUG")
    assert blank_settings.log_level == "DEBUG"


def test_r2_properties_default_to_none_and_not_configured(blank_settings):
    assert blank_settings.r2_bucket is None
    assert blank_settings.r2_endpoint_url is None
    assert blank_settings.r2_access_key_id is None
    assert blank_settings.r2_secret_access_key is None
    assert blank_settings.r2_configured is False


def test_r2_configured_true_when_all_four_set(blank_settings, monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "b")
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://x")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    assert blank_settings.r2_configured is True


def test_google_drive_properties_default_to_none_and_not_configured(blank_settings):
    assert blank_settings.google_drive_client_id is None
    assert blank_settings.google_drive_client_secret is None
    assert blank_settings.google_drive_refresh_token is None
    assert blank_settings.google_drive_configured is False


def test_google_drive_configured_true_when_all_three_set(blank_settings, monkeypatch):
    monkeypatch.setenv("GOOGLE_DRIVE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_DRIVE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_DRIVE_REFRESH_TOKEN", "token")
    assert blank_settings.google_drive_configured is True


def test_dashboard_auth_configured_false_when_incomplete(blank_settings, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    assert blank_settings.dashboard_auth_configured is False


def test_dashboard_auth_configured_true_when_complete(blank_settings, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://x/auth/callback")
    monkeypatch.setenv("DASHBOARD_ALLOWED_EMAIL", "chris@example.com")
    assert blank_settings.dashboard_auth_configured is True


def test_session_secret_missing_raises(blank_settings):
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        blank_settings.session_secret


def test_session_secret_present(blank_settings, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "shh")
    assert blank_settings.session_secret == "shh"


def test_internal_api_base_url_defaults_to_loopback(blank_settings):
    assert blank_settings.internal_api_base_url == f"http://127.0.0.1:{DEFAULT_HTTP_PORT}"


def test_internal_api_base_url_overridden(blank_settings, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_BASE_URL", "http://127.0.0.1:9000")
    assert blank_settings.internal_api_base_url == "http://127.0.0.1:9000"


def test_internal_api_key_defaults_to_none(blank_settings):
    assert blank_settings.internal_api_key is None


def test_migrations_dir_points_at_migrations_folder(blank_settings):
    assert blank_settings.migrations_dir.name == "migrations"
