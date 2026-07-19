"""service.py's users/API-key self-service functions — direct calls, no HTTP
layer (that's test_rest.py's job for the auth-gating half)."""

import asyncio
from http import HTTPStatus

import pytest

from chuk_experiments_server import service
from chuk_experiments_server.auth import AuthError
from chuk_experiments_server.constants import Scope, TokenProvider
from chuk_experiments_server.models import DashboardIdentity


def _admin() -> DashboardIdentity:
    return DashboardIdentity(email=None, role=Scope.ADMIN, user_id=None)


async def test_create_user_then_get_active_by_email():
    user = await service.create_user("reader@example.com", Scope.READ)
    assert user.role == Scope.READ

    fetched = await service.get_active_user_by_email("reader@example.com")
    assert fetched is not None
    assert fetched.id == user.id


async def test_get_active_user_by_email_unknown_returns_none():
    assert await service.get_active_user_by_email("nobody@example.com") is None


async def test_create_user_duplicate_email_raises_conflict():
    await service.create_user("dup@example.com", Scope.READ)
    with pytest.raises(service.ConflictError):
        await service.create_user("dup@example.com", Scope.WRITE)


async def test_list_team_users_includes_created_users():
    await service.create_user("a@example.com", Scope.READ)
    await service.create_user("b@example.com", Scope.WRITE)
    emails = {u.email for u in await service.list_team_users()}
    assert {"a@example.com", "b@example.com"} <= emails


async def test_revoke_user_marks_revoked_and_hides_from_active_lookup():
    user = await service.create_user("gone@example.com", Scope.READ)
    await service.revoke_user(user.id)
    assert await service.get_active_user_by_email("gone@example.com") is None


async def test_revoke_user_unknown_id_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.revoke_user(999999)


async def test_revoke_user_refuses_to_revoke_the_last_admin():
    """_dashboard_admin_user (conftest) always seeds exactly one active admin
    per test — find it dynamically rather than hardcoding its email."""
    users = await service.list_team_users()
    only_admin = next(u for u in users if u.role == Scope.ADMIN)

    with pytest.raises(service.ConflictError):
        await service.revoke_user(only_admin.id)
    assert await service.get_active_user_by_email(only_admin.email) is not None


async def test_revoke_user_allows_revoking_an_admin_when_another_remains():
    second_admin = await service.create_user("second-admin@example.com", Scope.ADMIN)
    await service.revoke_user(second_admin.id)
    assert await service.get_active_user_by_email("second-admin@example.com") is None


async def test_revoke_user_concurrent_last_two_admins_only_one_succeeds():
    """Two concurrent revoke_user calls against the only two active admins
    must not both pass the last-admin check — exactly one should succeed,
    the other should see the (by-then) last remaining admin and refuse.
    Verifies the end invariant (never zero active admins); asyncpg's real
    connections against local Postgres are fast enough that this doesn't
    reliably force the two transactions to overlap inside the same
    contention window the FOR UPDATE lock actually guards — it passed even
    against the pre-fix check-then-act code in manual testing. The FOR
    UPDATE fix itself is standard, timing-independent Postgres locking
    semantics; this test is a coarse correctness check, not proof the race
    was hit."""
    users = await service.list_team_users()
    first_admin = next(u for u in users if u.role == Scope.ADMIN)
    second_admin = await service.create_user("concurrent-admin@example.com", Scope.ADMIN)

    results = await asyncio.gather(
        service.revoke_user(first_admin.id),
        service.revoke_user(second_admin.id),
        return_exceptions=True,
    )

    conflicts = [r for r in results if isinstance(r, service.ConflictError)]
    successes = [r for r in results if r is None]
    assert len(conflicts) == 1
    assert len(successes) == 1

    remaining = [u for u in await service.list_team_users() if u.role == Scope.ADMIN and not u.revoked_at]
    assert len(remaining) == 1


async def test_revoke_user_cascades_to_their_api_keys():
    user = await service.create_user("owner@example.com", Scope.WRITE)
    identity = DashboardIdentity(email=user.email, role=user.role, user_id=user.id)
    created = await service.create_api_key(identity, "owner-key", [Scope.READ])

    await service.revoke_user(user.id)

    keys = await service.list_api_keys(_admin())
    revoked = next(k for k in keys if k.id == created.id)
    assert revoked.revoked_at is not None


async def test_create_api_key_rejects_scope_above_role_ceiling():
    identity = DashboardIdentity(email="w@example.com", role=Scope.WRITE, user_id=1)
    with pytest.raises(AuthError) as exc_info:
        await service.create_api_key(identity, "too-much", [Scope.ADMIN])
    assert exc_info.value.status_code == HTTPStatus.FORBIDDEN


async def test_create_api_key_allows_scope_within_role_ceiling():
    user = await service.create_user("writer@example.com", Scope.WRITE)
    identity = DashboardIdentity(email=user.email, role=user.role, user_id=user.id)
    created = await service.create_api_key(identity, "writer-key", [Scope.READ, Scope.WRITE])
    assert set(created.scopes) == {Scope.READ, Scope.WRITE}
    assert created.raw_key


async def test_create_api_key_bearer_admin_identity_has_no_ceiling():
    created = await service.create_api_key(_admin(), "admin-minted", [Scope.READ, Scope.WRITE, Scope.ADMIN])
    assert set(created.scopes) == {Scope.READ, Scope.WRITE, Scope.ADMIN}


async def test_list_api_keys_non_admin_sees_only_own():
    alice = await service.create_user("alice@example.com", Scope.WRITE)
    bob = await service.create_user("bob@example.com", Scope.WRITE)
    alice_identity = DashboardIdentity(email=alice.email, role=alice.role, user_id=alice.id)
    bob_identity = DashboardIdentity(email=bob.email, role=bob.role, user_id=bob.id)

    await service.create_api_key(alice_identity, "alice-key", [Scope.READ])
    await service.create_api_key(bob_identity, "bob-key", [Scope.READ])

    alice_keys = await service.list_api_keys(alice_identity)
    assert [k.name for k in alice_keys] == ["alice-key"]


async def test_list_api_keys_admin_sees_everyone():
    alice = await service.create_user("alice2@example.com", Scope.WRITE)
    alice_identity = DashboardIdentity(email=alice.email, role=alice.role, user_id=alice.id)
    await service.create_api_key(alice_identity, "alice2-key", [Scope.READ])

    admin_keys = await service.list_api_keys(_admin())
    assert "alice2-key" in {k.name for k in admin_keys}


async def test_revoke_api_key_owner_can_revoke_own():
    user = await service.create_user("owner2@example.com", Scope.WRITE)
    identity = DashboardIdentity(email=user.email, role=user.role, user_id=user.id)
    created = await service.create_api_key(identity, "self-revoke", [Scope.READ])

    await service.revoke_api_key(identity, created.id)

    keys = await service.list_api_keys(identity)
    assert keys[0].revoked_at is not None


async def test_revoke_api_key_non_owner_non_admin_raises_not_found():
    alice = await service.create_user("alice3@example.com", Scope.WRITE)
    bob = await service.create_user("bob3@example.com", Scope.WRITE)
    alice_identity = DashboardIdentity(email=alice.email, role=alice.role, user_id=alice.id)
    bob_identity = DashboardIdentity(email=bob.email, role=bob.role, user_id=bob.id)
    created = await service.create_api_key(alice_identity, "alices-key", [Scope.READ])

    with pytest.raises(service.NotFoundError):
        await service.revoke_api_key(bob_identity, created.id)


async def test_revoke_api_key_admin_can_revoke_anyones():
    alice = await service.create_user("alice4@example.com", Scope.WRITE)
    alice_identity = DashboardIdentity(email=alice.email, role=alice.role, user_id=alice.id)
    created = await service.create_api_key(alice_identity, "alices-key-2", [Scope.READ])

    await service.revoke_api_key(_admin(), created.id)

    keys = await service.list_api_keys(_admin())
    revoked = next(k for k in keys if k.id == created.id)
    assert revoked.revoked_at is not None


# --- Per-user GitHub/HF tokens ----------------------------------------------


@pytest.fixture(autouse=True)
def _token_encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    from chuk_experiments_server.config import settings

    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(type(settings), "token_encryption_key", property(lambda self: key))


async def test_set_and_get_user_token_round_trips():
    user = await service.create_user("tokenuser@example.com", Scope.WRITE)
    identity = DashboardIdentity(email=user.email, role=user.role, user_id=user.id)

    await service.set_user_token(identity, TokenProvider.GITHUB, "ghp_realvalue")

    assert await service.get_user_token(user.id, TokenProvider.GITHUB) == "ghp_realvalue"
    assert await service.get_user_token(user.id, TokenProvider.HUGGINGFACE) is None


async def test_get_user_token_status_reflects_set_tokens():
    user = await service.create_user("tokenuser2@example.com", Scope.WRITE)
    identity = DashboardIdentity(email=user.email, role=user.role, user_id=user.id)

    assert await service.get_user_token_status(user.id) == {
        "github_token_set": False,
        "huggingface_token_set": False,
    }

    await service.set_user_token(identity, TokenProvider.HUGGINGFACE, "hf_realvalue")

    assert await service.get_user_token_status(user.id) == {
        "github_token_set": False,
        "huggingface_token_set": True,
    }


async def test_get_user_token_status_none_user_id_is_all_false():
    assert await service.get_user_token_status(None) == {
        "github_token_set": False,
        "huggingface_token_set": False,
    }


async def test_clear_user_token_removes_it():
    user = await service.create_user("tokenuser3@example.com", Scope.WRITE)
    identity = DashboardIdentity(email=user.email, role=user.role, user_id=user.id)
    await service.set_user_token(identity, TokenProvider.GITHUB, "ghp_realvalue")

    await service.clear_user_token(identity, TokenProvider.GITHUB)

    assert await service.get_user_token(user.id, TokenProvider.GITHUB) is None


async def test_set_user_token_rejects_bearer_admin_session_with_no_user():
    with pytest.raises(service.ValidationError):
        await service.set_user_token(_admin(), TokenProvider.GITHUB, "x")


async def test_set_user_token_rejects_when_encryption_not_configured(monkeypatch):
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "token_encryption_key", property(lambda self: None))
    user = await service.create_user("tokenuser4@example.com", Scope.WRITE)
    identity = DashboardIdentity(email=user.email, role=user.role, user_id=user.id)

    with pytest.raises(service.ValidationError):
        await service.set_user_token(identity, TokenProvider.GITHUB, "x")


async def test_get_user_token_none_when_user_has_no_token():
    user = await service.create_user("tokenuser5@example.com", Scope.WRITE)
    assert await service.get_user_token(user.id, TokenProvider.GITHUB) is None


async def test_clear_user_token_rejects_bearer_admin_session_with_no_user():
    with pytest.raises(service.ValidationError):
        await service.clear_user_token(_admin(), TokenProvider.GITHUB)


async def test_get_user_token_status_unknown_user_id_is_all_false():
    assert await service.get_user_token_status(999999) == {
        "github_token_set": False,
        "huggingface_token_set": False,
    }
