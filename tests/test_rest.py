"""REST layer tests via an in-process ASGI transport (see conftest.api_client)
— exercises real Starlette routing/param parsing/handler code against the
disposable test Postgres, not just service.py directly. Focused on HTTP-
specific behavior (auth gating, status codes, param parsing, JSON shape);
combinatorial business-logic edge cases are covered in test_service_*.py."""

from http import HTTPStatus

from chuk_experiments_server import auth as auth_module


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _create_experiment(api_client, key, **overrides):
    body = {"programme": "cn", "slug": "cn-7", "title": "t", **overrides}
    return await api_client.post("/v1/experiments", json=body, headers=_auth(key))


async def _enqueue_run(api_client, key, slug="cn-7", **overrides):
    body = {"slug": "seed-0", **overrides}
    return await api_client.post(f"/v1/experiments/{slug}/runs", json=body, headers=_auth(key))


# --- Programmes --------------------------------------------------------------


async def test_programmes_requires_auth(api_client):
    resp = await api_client.get("/v1/programmes")
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


async def test_programmes_empty_list(api_client, write_key):
    resp = await api_client.get("/v1/programmes", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == []


async def test_programmes_accepts_dashboard_session_cookie_with_no_bearer_token(
    api_client, authenticated_cookies
):
    """A READ route must work for the dashboard SPA calling /v1/* directly
    with only its Google session cookie, no Authorization header at all."""
    resp = await api_client.get("/v1/programmes", cookies=authenticated_cookies)
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == []


async def test_create_experiment_rejects_cookie_only_request(api_client, authenticated_cookies):
    """The dashboard session cookie satisfies Scope.READ only — a WRITE
    route must still reject a request carrying just the cookie."""
    resp = await api_client.post(
        "/v1/experiments",
        json={"programme": "cn", "slug": "cn-7", "title": "t"},
        cookies=authenticated_cookies,
    )
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


# --- Experiments ---------------------------------------------------------------


async def test_create_experiment_requires_write_scope(api_client):
    await auth_module.upsert_bootstrap_key("readonly:read:readonly-rest-key")
    resp = await _create_experiment(api_client, "readonly-rest-key")
    assert resp.status_code == HTTPStatus.FORBIDDEN


async def test_create_experiment_then_get(api_client, write_key):
    create_resp = await _create_experiment(api_client, write_key)
    assert create_resp.status_code == HTTPStatus.CREATED
    assert create_resp.json()["slug"] == "cn-7"

    get_resp = await api_client.get("/v1/experiments/cn-7", headers=_auth(write_key))
    assert get_resp.status_code == HTTPStatus.OK
    assert get_resp.json()["title"] == "t"


async def test_create_experiment_duplicate_is_409(api_client, write_key):
    await _create_experiment(api_client, write_key)
    resp = await _create_experiment(api_client, write_key)
    assert resp.status_code == HTTPStatus.CONFLICT


async def test_get_experiment_missing_is_404(api_client, write_key):
    resp = await api_client.get("/v1/experiments/does-not-exist", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.NOT_FOUND


async def test_patch_experiment_updates_status(api_client, write_key):
    await _create_experiment(api_client, write_key)
    resp = await api_client.patch(
        "/v1/experiments/cn-7", json={"status": "completed"}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["status"] == "completed"


async def test_list_experiments_filters_by_programme(api_client, write_key):
    await _create_experiment(api_client, write_key, programme="cn", slug="cn-7")
    await _create_experiment(api_client, write_key, programme="div", slug="div-3")

    resp = await api_client.get("/v1/experiments", params={"programme": "div"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert [e["slug"] for e in resp.json()] == ["div-3"]


async def test_list_experiments_sort_title_ascending(api_client, write_key):
    await _create_experiment(api_client, write_key, slug="cn-b", title="B experiment")
    await _create_experiment(api_client, write_key, slug="cn-a", title="A experiment")

    resp = await api_client.get(
        "/v1/experiments", params={"sort": "title", "order": "asc"}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.OK
    assert [e["slug"] for e in resp.json()] == ["cn-a", "cn-b"]


async def test_list_experiments_rejects_unknown_sort_column(api_client, write_key):
    resp = await api_client.get("/v1/experiments", params={"sort": "nope"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_append_writeup(api_client, write_key):
    await _create_experiment(api_client, write_key)
    resp = await api_client.post(
        "/v1/experiments/cn-7/writeups", json={"body_md": "# hello"}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.json()["version"] == 1
    assert resp.json()["author"] == "pytest"  # the calling key's name, not client-supplied


# --- Search / index ------------------------------------------------------------


async def test_search_requires_at_least_one_filter(api_client, write_key):
    resp = await api_client.get("/v1/search", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_search_by_query(api_client, write_key):
    await _create_experiment(
        api_client, write_key, title="fingerprint embeddings", hypothesis="fingerprint signal"
    )
    resp = await api_client.get("/v1/search", params={"q": "fingerprint"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert [h["slug"] for h in resp.json()] == ["cn-7"]


async def test_search_offset_paginates_past_the_first_page(api_client, write_key):
    """The dashboard SPA pages search results via limit+offset (no separate
    count query) — confirm offset actually advances past already-seen rows
    rather than being silently ignored."""
    for i in range(3):
        await _create_experiment(api_client, write_key, slug=f"cn-{i}", status="running")

    first_page = await api_client.get(
        "/v1/search", params={"status": "running", "limit": 2}, headers=_auth(write_key)
    )
    second_page = await api_client.get(
        "/v1/search", params={"status": "running", "limit": 2, "offset": 2}, headers=_auth(write_key)
    )
    assert first_page.status_code == second_page.status_code == HTTPStatus.OK
    first_slugs = [h["slug"] for h in first_page.json()]
    second_slugs = [h["slug"] for h in second_page.json()]
    assert len(first_slugs) == 2
    assert len(second_slugs) == 1
    assert not set(first_slugs) & set(second_slugs)


async def test_index(api_client, write_key):
    await _create_experiment(api_client, write_key)
    resp = await api_client.get("/v1/index", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert [e["slug"] for e in resp.json()] == ["cn-7"]


# --- Runs ------------------------------------------------------------------


async def test_enqueue_run_and_get(api_client, write_key):
    await _create_experiment(api_client, write_key)
    enqueue_resp = await _enqueue_run(api_client, write_key)
    assert enqueue_resp.status_code == HTTPStatus.CREATED
    run_id = enqueue_resp.json()["id"]

    get_resp = await api_client.get(f"/v1/runs/{run_id}", headers=_auth(write_key))
    assert get_resp.status_code == HTTPStatus.OK
    assert get_resp.json()["slug"] == "seed-0"


async def test_get_run_missing_is_404(api_client, write_key):
    resp = await api_client.get("/v1/runs/999999", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.NOT_FOUND


async def test_patch_run_status(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.patch(
        f"/v1/runs/{run_id}", json={"status": "completed"}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["status"] == "completed"


async def test_cancel_run(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key, status="queued")).json()["id"]
    resp = await api_client.post(f"/v1/runs/{run_id}/cancel", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["status"] == "cancelled"


async def test_cancel_run_from_running_is_409(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key, status="running")).json()["id"]
    resp = await api_client.post(f"/v1/runs/{run_id}/cancel", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.CONFLICT


async def test_submit_result(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/results", json={"name": "acc", "value": 0.9}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.json()["submitted_by"] == "pytest"


async def test_register_artifact(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts",
        json={"kind": "checkpoint", "uri": "s3://bucket/ckpt.bin"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED


async def test_artifacts_presign_not_configured(api_client, write_key, monkeypatch):
    # R2 is genuinely configured in this dev environment's .env — force the
    # "not configured" branch deterministically rather than relying on ambient state.
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "r2_configured", property(lambda self: False))
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/presign", json={"filename": "x.bin"}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.NOT_IMPLEMENTED


async def test_artifact_download_not_configured(api_client, write_key, monkeypatch):
    from chuk_experiments_server.config import settings

    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    artifact_id = (
        await api_client.post(
            f"/v1/runs/{run_id}/artifacts",
            json={"kind": "checkpoint", "uri": "s3://bucket/ckpt.bin"},
            headers=_auth(write_key),
        )
    ).json()["id"]

    monkeypatch.setattr(type(settings), "r2_configured", property(lambda self: False))
    resp = await api_client.get(f"/v1/artifacts/{artifact_id}/download", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.NOT_IMPLEMENTED


async def test_artifacts_presign_configured(api_client, write_key, monkeypatch):
    """storage.presign_put itself isn't exercised here (that's real
    boto3/R2-credential territory, not this route's job) — mocked so the test
    is deterministic in CI, where no R2 secrets are set."""
    from chuk_experiments_server import storage
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "r2_configured", property(lambda self: True))
    monkeypatch.setattr(storage, "presign_put", lambda key, content_type=None: f"https://fake-r2/{key}")

    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/presign",
        json={"filename": "x.bin", "kind": "checkpoint"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED
    body = resp.json()
    assert body["upload_url"] == f"https://fake-r2/runs/{run_id}/checkpoint/x.bin"
    assert body["uri"] == f"s3://{settings.r2_bucket}/runs/{run_id}/checkpoint/x.bin"


async def test_artifacts_upload_not_configured(api_client, write_key, monkeypatch):
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: False))
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload",
        json={"filename": "x.txt", "kind": "other", "content_base64": "aGVsbG8="},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.NOT_IMPLEMENTED


async def test_artifacts_upload_configured_success(api_client, write_key, monkeypatch):
    """drive_storage's actual Drive-API calls aren't exercised here (that's
    real OAuth/Drive-credential territory, not this route's job) — mocked
    so the test is deterministic in CI, where no Drive secrets are set."""
    from chuk_experiments_server import drive_storage
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: True))
    monkeypatch.setattr(drive_storage, "get_client", lambda: "fake-service")
    monkeypatch.setattr(drive_storage, "ensure_folder", lambda service, name, parent_id: "root-folder-id")
    monkeypatch.setattr(drive_storage, "ensure_folder_path", lambda service, root_id, parts: "leaf-folder-id")
    monkeypatch.setattr(
        drive_storage, "upload_bytes", lambda service, filename, content, parent_id: "fake-file-id"
    )

    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload",
        json={"filename": "tokenizer_bench.py", "kind": "other", "content_base64": "aGVsbG8="},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED
    body = resp.json()
    assert body["uri"] == "gdrive://fake-file-id"
    assert body["meta"]["source_path"] == "tokenizer_bench.py"
    assert "drive_url" in body["meta"]


async def test_artifacts_upload_rejects_invalid_base64(api_client, write_key, monkeypatch):
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: True))
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload",
        json={"filename": "x.txt", "kind": "other", "content_base64": "not-valid-base64!!!"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_artifacts_upload_rejects_oversized_content(api_client, write_key, monkeypatch):
    from chuk_experiments_server import rest
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: True))
    monkeypatch.setattr(rest, "_MAX_UPLOAD_BYTES", 4)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload",
        json={"filename": "x.txt", "kind": "other", "content_base64": "aGVsbG8="},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_artifact_download_configured_redirects(api_client, write_key, monkeypatch):
    from chuk_experiments_server import storage
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "r2_configured", property(lambda self: True))
    monkeypatch.setattr(storage, "presign_get", lambda key: "https://fake-r2/signed-get")

    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    artifact_id = (
        await api_client.post(
            f"/v1/runs/{run_id}/artifacts",
            json={"kind": "checkpoint", "uri": f"s3://{settings.r2_bucket}/runs/{run_id}/checkpoint/x.bin"},
            headers=_auth(write_key),
        )
    ).json()["id"]

    resp = await api_client.get(
        f"/v1/artifacts/{artifact_id}/download", headers=_auth(write_key), follow_redirects=False
    )
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "https://fake-r2/signed-get"


async def test_artifact_download_gdrive_redirects_without_r2(api_client, write_key, monkeypatch):
    """A Drive-archived artifact (scripts/archive_*_to_drive.py) redirects
    straight to its stored drive_url — no presigning, no R2 configuration
    required at all, unlike an s3:// artifact."""
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "r2_configured", property(lambda self: False))

    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    artifact_id = (
        await api_client.post(
            f"/v1/runs/{run_id}/artifacts",
            json={
                "kind": "other",
                "uri": "gdrive://abc123",
                "meta": {"drive_url": "https://drive.google.com/drive/folders/abc123"},
            },
            headers=_auth(write_key),
        )
    ).json()["id"]

    resp = await api_client.get(
        f"/v1/artifacts/{artifact_id}/download", headers=_auth(write_key), follow_redirects=False
    )
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "https://drive.google.com/drive/folders/abc123"


async def test_runs_compare(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    await api_client.post(
        f"/v1/runs/{run_id}/results", json={"name": "acc", "value": 0.5}, headers=_auth(write_key)
    )

    resp = await api_client.get(
        "/v1/runs/compare", params={"ids": [run_id], "metric": "acc"}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()[0]["value"] == 0.5


async def test_runs_compare_missing_params_is_400(api_client, write_key):
    resp = await api_client.get("/v1/runs/compare", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_artifacts_collection_find_checkpoints(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    await api_client.post(
        f"/v1/runs/{run_id}/artifacts",
        json={"kind": "checkpoint", "uri": "s3://bucket/ckpt.bin"},
        headers=_auth(write_key),
    )
    resp = await api_client.get("/v1/artifacts", params={"kind": "checkpoint"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert len(resp.json()) == 1


# --- Queue -------------------------------------------------------------------


async def test_queue_peek_and_claim(api_client, write_key):
    await _create_experiment(api_client, write_key)
    await _enqueue_run(api_client, write_key, status="queued")

    peek_resp = await api_client.get("/v1/queue", headers=_auth(write_key))
    assert peek_resp.status_code == HTTPStatus.OK
    assert len(peek_resp.json()) == 1

    claim_resp = await api_client.post(
        "/v1/queue/claim",
        json={"backend": "any", "session_seconds": 600, "claimed_by": "tester"},
        headers=_auth(write_key),
    )
    assert claim_resp.status_code == HTTPStatus.CREATED
    assert claim_resp.json()[0]["status"] == "claimed"


async def test_queue_sweep_requires_admin_scope(api_client):
    await auth_module.upsert_bootstrap_key("writer-only:read|write:writer-only-key")
    resp = await api_client.post("/v1/queue/sweep", headers=_auth("writer-only-key"))
    assert resp.status_code == HTTPStatus.FORBIDDEN


async def test_run_lease_renewal(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key, status="queued")).json()["id"]
    await api_client.post(
        "/v1/queue/claim",
        json={"backend": "any", "session_seconds": 600, "claimed_by": "tester"},
        headers=_auth(write_key),
    )
    resp = await api_client.post(f"/v1/runs/{run_id}/lease", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["status"] == "running"


# --- Dashboard users & self-service API keys ----------------------------------


async def _cookie_for(email: str, role: str) -> dict:
    from chuk_experiments_server import webauth
    from chuk_experiments_server.constants import SESSION_COOKIE_NAME
    from chuk_experiments_server.constants import Scope as _Scope

    await auth_module.upsert_bootstrap_user(email, _Scope(role))
    return {SESSION_COOKIE_NAME: webauth.create_session_cookie_value(email)}


async def test_me_bearer_admin(api_client, write_key):
    resp = await api_client.get("/v1/me", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"email": None, "role": "admin"}


async def test_me_cookie_reflects_signed_in_users_role(api_client):
    cookies = await _cookie_for("me-reader@example.com", "read")
    resp = await api_client.get("/v1/me", cookies=cookies)
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"email": "me-reader@example.com", "role": "read"}


async def test_users_collection_requires_admin_role(api_client):
    cookies = await _cookie_for("plain-reader@example.com", "read")
    resp = await api_client.get("/v1/users", cookies=cookies)
    assert resp.status_code == HTTPStatus.FORBIDDEN


async def test_users_collection_admin_can_create_and_list(api_client, write_key):
    resp = await api_client.post(
        "/v1/users", json={"email": "newbie@example.com", "role": "write"}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.json()["role"] == "write"

    list_resp = await api_client.get("/v1/users", headers=_auth(write_key))
    assert "newbie@example.com" in {u["email"] for u in list_resp.json()}


async def test_users_item_revoke_requires_admin_role(api_client):
    cookies = await _cookie_for("cant-revoke@example.com", "write")
    resp = await api_client.delete("/v1/users/999999", cookies=cookies)
    assert resp.status_code == HTTPStatus.FORBIDDEN


async def test_users_item_revoke_cascades_their_keys(api_client, write_key):
    create_resp = await api_client.post(
        "/v1/users", json={"email": "temp@example.com", "role": "write"}, headers=_auth(write_key)
    )
    user_id = create_resp.json()["id"]
    cookies = await _cookie_for("temp@example.com", "write")
    key_resp = await api_client.post(
        "/v1/keys", json={"name": "temp-key", "scopes": ["read"]}, cookies=cookies
    )
    key_id = key_resp.json()["id"]

    revoke_resp = await api_client.delete(f"/v1/users/{user_id}", headers=_auth(write_key))
    assert revoke_resp.status_code == HTTPStatus.OK

    keys_resp = await api_client.get("/v1/keys", headers=_auth(write_key))
    revoked_key = next(k for k in keys_resp.json() if k["id"] == key_id)
    assert revoked_key["revoked_at"] is not None


async def test_users_item_refuses_to_revoke_last_admin(api_client, write_key):
    users_resp = await api_client.get("/v1/users", headers=_auth(write_key))
    only_admin = next(u for u in users_resp.json() if u["role"] == "admin")

    resp = await api_client.delete(f"/v1/users/{only_admin['id']}", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.CONFLICT


async def test_keys_collection_rejects_scope_above_role_ceiling(api_client):
    cookies = await _cookie_for("capped-writer@example.com", "write")
    resp = await api_client.post("/v1/keys", json={"name": "too-much", "scopes": ["admin"]}, cookies=cookies)
    assert resp.status_code == HTTPStatus.FORBIDDEN


async def test_keys_collection_allows_scope_within_role_ceiling(api_client):
    cookies = await _cookie_for("in-band-writer@example.com", "write")
    resp = await api_client.post(
        "/v1/keys", json={"name": "in-band", "scopes": ["read", "write"]}, cookies=cookies
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert "raw_key" in resp.json()


async def test_keys_collection_non_admin_sees_only_own(api_client):
    alice_cookies = await _cookie_for("rest-alice@example.com", "write")
    bob_cookies = await _cookie_for("rest-bob@example.com", "write")
    await api_client.post("/v1/keys", json={"name": "alice-key", "scopes": ["read"]}, cookies=alice_cookies)
    await api_client.post("/v1/keys", json={"name": "bob-key", "scopes": ["read"]}, cookies=bob_cookies)

    resp = await api_client.get("/v1/keys", cookies=alice_cookies)
    assert [k["name"] for k in resp.json()] == ["alice-key"]


async def test_keys_item_owner_can_revoke_own(api_client):
    cookies = await _cookie_for("self-revoker@example.com", "write")
    create_resp = await api_client.post(
        "/v1/keys", json={"name": "revoke-me", "scopes": ["read"]}, cookies=cookies
    )
    key_id = create_resp.json()["id"]

    resp = await api_client.delete(f"/v1/keys/{key_id}", cookies=cookies)
    assert resp.status_code == HTTPStatus.OK


async def test_keys_item_non_owner_non_admin_gets_404(api_client):
    owner_cookies = await _cookie_for("rest-owner@example.com", "write")
    other_cookies = await _cookie_for("rest-other@example.com", "write")
    create_resp = await api_client.post(
        "/v1/keys", json={"name": "owners-key", "scopes": ["read"]}, cookies=owner_cookies
    )
    key_id = create_resp.json()["id"]

    resp = await api_client.delete(f"/v1/keys/{key_id}", cookies=other_cookies)
    assert resp.status_code == HTTPStatus.NOT_FOUND
