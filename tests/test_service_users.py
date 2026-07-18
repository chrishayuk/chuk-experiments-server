"""service.py's users/API-key self-service functions — direct calls, no HTTP
layer (that's test_rest.py's job for the auth-gating half)."""

from http import HTTPStatus

import pytest

from chuk_experiments_server import service
from chuk_experiments_server.auth import AuthError
from chuk_experiments_server.constants import Scope
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
