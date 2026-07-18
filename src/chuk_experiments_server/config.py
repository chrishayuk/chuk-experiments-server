"""Environment-based settings. No framework — just os.environ with defaults."""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            break
except ImportError:
    pass


class Settings:
    @property
    def database_url(self) -> str:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        return url

    @property
    def bootstrap_key(self) -> str | None:
        """Format: name:scope1|scope2:rawkey — creates/refreshes an api_key row on migrate."""
        return os.environ.get("EXPERIMENTS_BOOTSTRAP_KEY")

    @property
    def log_level(self) -> str:
        return os.environ.get("MCP_LOG_LEVEL", "INFO")

    @property
    def migrations_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / "migrations"

    # --- R2 / object storage (spec §9, Phase 2) -----------------------------
    # Shared with gpu-training-harness's artifact bucket rather than a
    # dedicated one — see CHUK_TRAIN_ARTIFACTS in that project's fly.toml.

    @property
    def r2_bucket(self) -> str | None:
        return os.environ.get("R2_BUCKET")

    @property
    def r2_endpoint_url(self) -> str | None:
        return os.environ.get("R2_ENDPOINT_URL")

    @property
    def r2_access_key_id(self) -> str | None:
        return os.environ.get("R2_ACCESS_KEY_ID")

    @property
    def r2_secret_access_key(self) -> str | None:
        return os.environ.get("R2_SECRET_ACCESS_KEY")

    @property
    def r2_configured(self) -> bool:
        return bool(self.r2_bucket and self.r2_endpoint_url and self.r2_access_key_id and self.r2_secret_access_key)


settings = Settings()
