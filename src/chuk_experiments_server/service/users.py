"""Users & self-service API keys (dashboard team management), plus per-user
GitHub/HF tokens (external artifact verification).

Single seeded 'default' team for now (Chris's call — "saves us refactoring
later" rather than building multi-team support before it's needed): every
query below implicitly operates within it. Adding real team-switching later
means adding a team_id filter here, not a schema change.

Per-user tokens are human/dashboard-only, same as key self-service — no MCP
tool wraps these, same reasoning as create_api_key having none: this is a
one-time personal setup action, not something an agent should be doing on a
user's behalf.
"""

from http import HTTPStatus

import asyncpg

from .. import token_crypto
from ..auth import AuthError, generate_key, hash_key
from ..config import settings
from ..constants import ROLE_SCOPE_CEILING, Scope, TokenProvider
from ..db import get_pool
from ..models import ApiKeyCreateResponse, ApiKeySummary, AppUser, DashboardIdentity
from ._shared import ConflictError, NotFoundError, ValidationError


async def get_active_user_by_email(email: str) -> AppUser | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, role, created_at, revoked_at FROM app_user WHERE email = $1 AND revoked_at IS NULL",
        email,
    )
    return AppUser.model_validate(dict(row)) if row else None


async def list_team_users() -> list[AppUser]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, email, role, created_at, revoked_at FROM app_user ORDER BY created_at"
    )
    return [AppUser.model_validate(dict(row)) for row in rows]


async def create_user(email: str, role: Scope) -> AppUser:
    pool = await get_pool()
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO app_user (team_id, email, role)
            VALUES ((SELECT id FROM team WHERE slug = 'default'), $1, $2)
            RETURNING id, email, role, created_at, revoked_at
            """,
            email,
            role.value,
        )
    except asyncpg.UniqueViolationError:
        raise ConflictError(f"A user with email '{email}' already exists") from None
    return AppUser.model_validate(dict(row))


async def revoke_user(user_id: int) -> None:
    """Soft-revokes the user and cascades to their own API keys — a removed
    collaborator shouldn't leave live credentials behind. Refuses to revoke
    the last remaining active admin: that would leave the team with no one
    able to sign in and manage users/keys through the dashboard at all
    (short of the bearer-ADMIN CLI escape hatch, which isn't a substitute
    for a real admin user)."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        target = await conn.fetchrow(
            "SELECT role FROM app_user WHERE id = $1 AND revoked_at IS NULL", user_id
        )
        if target is None:
            raise NotFoundError(f"No active user with id {user_id}")

        if target["role"] == Scope.ADMIN.value:
            # FOR UPDATE locks every active admin row for the transaction's
            # duration — a concurrent revoke_user targeting a *different*
            # admin blocks here instead of racing this one on a stale
            # count, which is what let two concurrent revokes both pass
            # the check and leave zero active admins. (Postgres rejects
            # FOR UPDATE combined with an aggregate, hence counting the
            # fetched rows in Python rather than `SELECT count(*) ... FOR
            # UPDATE`.)
            admin_rows = await conn.fetch(
                "SELECT id FROM app_user WHERE role = $1 AND revoked_at IS NULL FOR UPDATE",
                Scope.ADMIN.value,
            )
            remaining_admins = sum(1 for row in admin_rows if row["id"] != user_id)
            if remaining_admins == 0:
                raise ConflictError("Cannot revoke the last remaining admin user")

        await conn.execute("UPDATE app_user SET revoked_at = now() WHERE id = $1", user_id)
        await conn.execute(
            "UPDATE api_key SET revoked_at = now() WHERE created_by_user_id = $1 AND revoked_at IS NULL",
            user_id,
        )


async def list_api_keys(caller: DashboardIdentity) -> list[ApiKeySummary]:
    """Admins (including the bearer-ADMIN "system operator", user_id=None)
    see every key on the team; anyone else sees only their own."""
    pool = await get_pool()
    if caller.role == Scope.ADMIN:
        rows = await pool.fetch(
            """
            SELECT k.id, k.name, k.scopes, k.created_at, k.revoked_at, u.email AS created_by_email
            FROM api_key k
            LEFT JOIN app_user u ON u.id = k.created_by_user_id
            ORDER BY k.created_at DESC
            """
        )
    else:
        rows = await pool.fetch(
            """
            SELECT k.id, k.name, k.scopes, k.created_at, k.revoked_at, u.email AS created_by_email
            FROM api_key k
            LEFT JOIN app_user u ON u.id = k.created_by_user_id
            WHERE k.created_by_user_id = $1
            ORDER BY k.created_at DESC
            """,
            caller.user_id,
        )
    return [ApiKeySummary.model_validate(dict(row)) for row in rows]


async def create_api_key(caller: DashboardIdentity, name: str, scopes: list[Scope]) -> ApiKeyCreateResponse:
    """Self-service key minting — `scopes` is capped at the caller's own role
    ceiling (see ROLE_SCOPE_CEILING), so a "write"-role user can never mint
    themselves an admin-scoped key. Returns the raw key once, same
    "shown only now" contract as the CLI's `keys create`."""
    excess = set(scopes) - ROLE_SCOPE_CEILING[caller.role]
    if excess:
        raise AuthError(
            f"role '{caller.role.value}' cannot mint scope(s): {', '.join(sorted(s.value for s in excess))}",
            status_code=HTTPStatus.FORBIDDEN,
        )
    raw = generate_key()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO api_key (key_hash, name, scopes, team_id, created_by_user_id)
        VALUES ($1, $2, $3, (SELECT id FROM team WHERE slug = 'default'), $4)
        RETURNING id, name, scopes, created_at
        """,
        hash_key(raw),
        name,
        [s.value for s in scopes],
        caller.user_id,
    )
    return ApiKeyCreateResponse.model_validate({**dict(row), "raw_key": raw})


async def revoke_api_key(caller: DashboardIdentity, key_id: int) -> None:
    pool = await get_pool()
    if caller.role == Scope.ADMIN:
        row = await pool.fetchrow(
            "UPDATE api_key SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL RETURNING id",
            key_id,
        )
    else:
        row = await pool.fetchrow(
            """
            UPDATE api_key SET revoked_at = now()
            WHERE id = $1 AND created_by_user_id = $2 AND revoked_at IS NULL
            RETURNING id
            """,
            key_id,
            caller.user_id,
        )
    if row is None:
        raise NotFoundError(f"No api key with id {key_id}")


_TOKEN_COLUMN: dict[TokenProvider, str] = {
    TokenProvider.GITHUB: "github_token_encrypted",
    TokenProvider.HUGGINGFACE: "huggingface_token_encrypted",
}


async def set_user_token(caller: DashboardIdentity, provider: TokenProvider, raw_token: str) -> None:
    if caller.user_id is None:
        raise ValidationError(
            "Personal tokens require a signed-in dashboard user, not a bearer-admin session."
        )
    if not settings.token_encryption_configured:
        raise ValidationError("TOKEN_ENCRYPTION_KEY is not configured on this server.")
    encrypted = token_crypto.encrypt_token(raw_token)
    pool = await get_pool()
    column = _TOKEN_COLUMN[provider]
    await pool.execute(f"UPDATE app_user SET {column} = $1 WHERE id = $2", encrypted, caller.user_id)  # noqa: S608 - column is a fixed enum-keyed lookup, never caller input


async def clear_user_token(caller: DashboardIdentity, provider: TokenProvider) -> None:
    if caller.user_id is None:
        raise ValidationError(
            "Personal tokens require a signed-in dashboard user, not a bearer-admin session."
        )
    pool = await get_pool()
    column = _TOKEN_COLUMN[provider]
    await pool.execute(f"UPDATE app_user SET {column} = NULL WHERE id = $1", caller.user_id)  # noqa: S608 - column is a fixed enum-keyed lookup, never caller input


async def get_user_token_status(user_id: int | None) -> dict[str, bool]:
    if user_id is None:
        return {"github_token_set": False, "huggingface_token_set": False}
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT github_token_encrypted, huggingface_token_encrypted FROM app_user WHERE id = $1", user_id
    )
    if row is None:
        return {"github_token_set": False, "huggingface_token_set": False}
    return {
        "github_token_set": row["github_token_encrypted"] is not None,
        "huggingface_token_set": row["huggingface_token_encrypted"] is not None,
    }


async def get_user_token(user_id: int | None, provider: TokenProvider) -> str | None:
    """Only used by verify_artifact's token resolution — never exposed over
    REST, unlike get_user_token_status."""
    if user_id is None:
        return None
    pool = await get_pool()
    column = _TOKEN_COLUMN[provider]
    encrypted = await pool.fetchval(f"SELECT {column} FROM app_user WHERE id = $1", user_id)  # noqa: S608 - column is a fixed enum-keyed lookup, never caller input
    return token_crypto.decrypt_token(encrypted) if encrypted else None
