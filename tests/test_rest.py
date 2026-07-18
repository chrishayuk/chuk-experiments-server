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
