"""Environment-based settings. No framework — just os.environ with defaults."""

import os
from pathlib import Path

from .constants import DEFAULT_HTTP_PORT

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
    # Own dedicated bucket ("chuk-experiments"), same Cloudflare account as
    # gpu-training-harness but a separate bucket and a separate R2 API token
    # scoped only to it — training-harness artifacts (bucket "chuk-train",
    # see CHUK_TRAIN_ARTIFACTS in that project's fly.toml) live apart from
    # this server's, on purpose.

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
        return bool(
            self.r2_bucket and self.r2_endpoint_url and self.r2_access_key_id and self.r2_secret_access_key
        )

    # --- Google Drive artifact storage ---------------------------------------
    # For artifacts an agent has bytes for right now (config/log/small dataset
    # files) rather than something already sitting in R2 — see
    # drive_storage.py. Reuses gpu-training-harness's existing OAuth client +
    # a drive.file-scoped refresh token, same credentials the standalone
    # archive_*_to_drive.py scripts already use locally.

    @property
    def google_drive_client_id(self) -> str | None:
        return os.environ.get("GOOGLE_DRIVE_CLIENT_ID")

    @property
    def google_drive_client_secret(self) -> str | None:
        return os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET")

    @property
    def google_drive_refresh_token(self) -> str | None:
        return os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN")

    @property
    def google_drive_configured(self) -> bool:
        return bool(
            self.google_drive_client_id
            and self.google_drive_client_secret
            and self.google_drive_refresh_token
        )

    # --- Dashboard auth (Phase 4) --------------------------------------------
    # Reuses chuk-mcp-stage's Google OAuth client (same Client ID/Secret) —
    # its authorized redirect URIs just need this server's callback URL added
    # in Google Cloud Console. Scope requested here is basic sign-in
    # (openid email profile), not chuk-mcp-stage's Drive access.

    @property
    def google_client_id(self) -> str | None:
        return os.environ.get("GOOGLE_CLIENT_ID")

    @property
    def google_client_secret(self) -> str | None:
        return os.environ.get("GOOGLE_CLIENT_SECRET")

    @property
    def google_redirect_uri(self) -> str | None:
        return os.environ.get("GOOGLE_REDIRECT_URI")

    @property
    def dashboard_allowed_email(self) -> str | None:
        return os.environ.get("DASHBOARD_ALLOWED_EMAIL")

    @property
    def session_secret(self) -> str:
        secret = os.environ.get("SESSION_SECRET")
        if not secret:
            raise RuntimeError("SESSION_SECRET is not set")
        return secret

    @property
    def dashboard_auth_configured(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret
            and self.google_redirect_uri
            and self.dashboard_allowed_email
        )

    # --- External artifact reference verification (external_refs.py) --------
    # Both optional, and both a *fallback* only — service.verify_artifact
    # prefers the requesting user's own stored token (see below) and only
    # falls back to these server-wide env vars for keys with no owning user
    # (bootstrap/CI keys) or users who haven't set a personal one yet.
    # Unauthenticated GitHub API access is 60 req/hr, which a personal access
    # token (no scopes needed, just raises the rate limit) bumps to 5000/hr;
    # unset works fine, just rate-limited. Hugging Face's public repo tree
    # API needs no auth at all for public repos — this is only for verifying
    # private ones.

    @property
    def github_token(self) -> str | None:
        return os.environ.get("GITHUB_TOKEN")

    @property
    def huggingface_token(self) -> str | None:
        return os.environ.get("HUGGINGFACE_TOKEN")

    # --- Per-user token encryption (token_crypto.py) -------------------------
    # A Fernet key (Fernet.generate_key(), base64 urlsafe) used to encrypt
    # app_user.github_token_encrypted/huggingface_token_encrypted at rest —
    # the first reversible secret this schema stores (api_key only ever
    # stores a one-way hash). Optional like everything else here: unset means
    # set_user_token refuses with a clear "not configured" error rather than
    # storing anything insecurely.

    @property
    def token_encryption_key(self) -> str | None:
        return os.environ.get("TOKEN_ENCRYPTION_KEY")

    @property
    def token_encryption_configured(self) -> bool:
        return bool(self.token_encryption_key)

    # --- Internal API access (tools/) --------------------------------------
    # MCP tools call this server's own REST API over HTTP rather than
    # service/ directly — see internal_client.py.

    @property
    def internal_api_base_url(self) -> str:
        return os.environ.get("INTERNAL_API_BASE_URL", f"http://127.0.0.1:{DEFAULT_HTTP_PORT}")

    @property
    def internal_api_key(self) -> str | None:
        """Unused by any code path since the dashboard was rewritten as a
        client-side SPA — the browser now calls /v1/* directly via its own
        Google session cookie (auth.require_dashboard_role /
        require_scope_from_request), no server-side proxy layer to
        authenticate. Kept only so existing Fly secrets/test fixtures don't
        need touching for this alone; safe to remove entirely in a future
        cleanup pass. MCP tools never used this either — they forward the
        calling agent's own key instead (see tools/)."""
        return os.environ.get("INTERNAL_API_KEY")


settings = Settings()
