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


settings = Settings()
