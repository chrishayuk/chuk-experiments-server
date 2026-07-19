"""REST layer tests via an in-process ASGI transport (see conftest.api_client)
— exercises real Starlette routing/param parsing/handler code against the
disposable test Postgres, not just service.py directly. Focused on HTTP-
specific behavior (auth gating, status codes, param parsing, JSON shape);
combinatorial business-logic edge cases are covered in test_service_*.py."""

import json
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


async def test_unmapped_exception_is_logged_before_500(api_client, write_key, monkeypatch, caplog):
    """An exception error_payload can't map to a specific status must not
    vanish silently — it's the one case where the real traceback would
    otherwise never appear anywhere, unlike NotFoundError/ConflictError/
    etc., which are expected control flow with their own 4xx status."""
    from chuk_experiments_server import service

    async def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(service, "list_programmes", _boom)

    with caplog.at_level("ERROR", logger="chuk_experiments_server.rest"):
        resp = await api_client.get("/v1/programmes", headers=_auth(write_key))

    assert resp.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert resp.json() == {"error": "internal_error"}
    assert "kaboom" in caplog.text
    assert "Unhandled exception" in caplog.text


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


async def test_list_experiments_rejects_non_numeric_limit(api_client, write_key):
    resp = await api_client.get("/v1/experiments", params={"limit": "abc"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_list_experiments_rejects_negative_limit(api_client, write_key):
    resp = await api_client.get("/v1/experiments", params={"limit": "-1"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_list_experiments_clamps_huge_limit(api_client, write_key):
    for i in range(3):
        await _create_experiment(api_client, write_key, slug=f"cn-{i}")
    resp = await api_client.get("/v1/experiments", params={"limit": "1000000"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert len(resp.json()) == 3


async def test_list_experiments_rejects_non_numeric_offset(api_client, write_key):
    resp = await api_client.get("/v1/experiments", params={"offset": "abc"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_list_experiments_rejects_negative_offset(api_client, write_key):
    resp = await api_client.get("/v1/experiments", params={"offset": "-1"}, headers=_auth(write_key))
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


async def test_index_respects_limit_and_offset(api_client, write_key):
    for i in range(3):
        await _create_experiment(api_client, write_key, slug=f"cn-{i}")

    first_page = await api_client.get("/v1/index", params={"limit": 2}, headers=_auth(write_key))
    second_page = await api_client.get(
        "/v1/index", params={"limit": 2, "offset": 2}, headers=_auth(write_key)
    )
    assert len(first_page.json()) == 2
    assert len(second_page.json()) == 1


async def test_index_rejects_non_numeric_limit(api_client, write_key):
    resp = await api_client.get("/v1/index", params={"limit": "abc"}, headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


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


async def test_register_artifact_accepts_git_uri(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts",
        json={
            "kind": "other",
            "uri": "git+https://github.com/chrishayuk/chuk-mlx@abc123",
            "meta": {"git_repo": "chrishayuk/chuk-mlx", "git_commit": "abc123"},
        },
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED


async def test_register_artifact_accepts_hf_uri(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts",
        json={"kind": "checkpoint", "uri": "hf://model/chrishayuk/granite-4.1-3b-q4k-vindex@main"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED


def _mock_verify(monkeypatch, status="verified", detail="ok"):
    from chuk_experiments_server import external_refs

    async def _fake_verify_git_ref(*args, **kwargs):
        return external_refs.VerifyResult(status, detail)

    async def _fake_verify_hf_ref(*args, **kwargs):
        return external_refs.VerifyResult(status, detail)

    monkeypatch.setattr(external_refs, "verify_git_ref", _fake_verify_git_ref)
    monkeypatch.setattr(external_refs, "verify_hf_ref", _fake_verify_hf_ref)


async def test_artifact_verify_writes_status_and_returns_artifact(api_client, write_key, monkeypatch):
    _mock_verify(monkeypatch, status="verified", detail="commit exists")
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    artifact_id = (
        await api_client.post(
            f"/v1/runs/{run_id}/artifacts",
            json={"kind": "other", "uri": "git+https://github.com/chrishayuk/chuk-mlx@abc123"},
            headers=_auth(write_key),
        )
    ).json()["id"]

    resp = await api_client.post(f"/v1/artifacts/{artifact_id}/verify", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    body = resp.json()
    assert body["verify_status"] == "verified"
    assert body["verify_detail"] == "commit exists"
    assert body["verified_at"] is not None


async def test_artifact_verify_dispatches_hf_uri_to_hf_verify(api_client, write_key, monkeypatch):
    _mock_verify(monkeypatch, status="missing", detail="only 2.6GB of 36.5GB present")
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    artifact_id = (
        await api_client.post(
            f"/v1/runs/{run_id}/artifacts",
            json={"kind": "checkpoint", "uri": "hf://model/chrishayuk/granite-4.1-30b-q4k-vindex@main"},
            headers=_auth(write_key),
        )
    ).json()["id"]

    resp = await api_client.post(f"/v1/artifacts/{artifact_id}/verify", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["verify_status"] == "missing"


async def test_artifact_verify_rejects_non_reference_artifact(api_client, write_key, monkeypatch):
    _mock_verify(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    artifact_id = (
        await api_client.post(
            f"/v1/runs/{run_id}/artifacts",
            json={"kind": "checkpoint", "uri": "s3://bucket/ckpt.bin"},
            headers=_auth(write_key),
        )
    ).json()["id"]

    resp = await api_client.post(f"/v1/artifacts/{artifact_id}/verify", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_artifact_verify_404_on_unknown_id(api_client, write_key, monkeypatch):
    _mock_verify(monkeypatch)
    resp = await api_client.post("/v1/artifacts/999999/verify", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.NOT_FOUND


async def test_artifact_verify_requires_write_scope(api_client, monkeypatch):
    _mock_verify(monkeypatch)
    await auth_module.upsert_bootstrap_key("readonly:read:readonly-verify-key")
    resp = await api_client.post("/v1/artifacts/1/verify", headers=_auth("readonly-verify-key"))
    assert resp.status_code == HTTPStatus.FORBIDDEN


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
        json={"filename": "x.txt", "kind": "other", "content_base64": "aGVsbG8=", "name": "x"},
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
        json={
            "filename": "tokenizer_bench.py",
            "kind": "other",
            "content_base64": "aGVsbG8=",
            "name": "tokenizer_bench.py",
        },
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED
    body = resp.json()
    assert body["uri"] == "gdrive://fake-file-id"
    assert body["meta"]["source_path"] == "tokenizer_bench.py"
    assert "drive_url" in body["meta"]
    assert body["name"] == "tokenizer_bench.py"
    assert body["role"] == "produced"


async def test_artifacts_upload_dedups_by_name_and_sha(api_client, write_key, monkeypatch):
    """A second upload of the same (name, sha256) — as if a harness reused
    across TOK-1..TOK-5 were uploaded again — must reuse the first upload's
    Drive file instead of uploading a second time."""
    from chuk_experiments_server import drive_storage
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: True))
    monkeypatch.setattr(drive_storage, "get_client", lambda: "fake-service")
    monkeypatch.setattr(drive_storage, "ensure_folder", lambda service, name, parent_id: "root-folder-id")
    monkeypatch.setattr(drive_storage, "ensure_folder_path", lambda service, root_id, parts: "leaf-folder-id")
    upload_calls = []
    monkeypatch.setattr(
        drive_storage,
        "upload_bytes",
        lambda service, filename, content, parent_id: upload_calls.append(filename) or "fake-file-id",
    )

    await _create_experiment(api_client, write_key)
    run_a = (await _enqueue_run(api_client, write_key)).json()["id"]
    run_b_resp = await api_client.post(
        "/v1/experiments/cn-7/runs", json={"slug": "seed-1"}, headers=_auth(write_key)
    )
    run_b = run_b_resp.json()["id"]

    body = {"filename": "harness.py", "kind": "other", "content_base64": "aGVsbG8=", "name": "harness.py"}
    first = await api_client.post(f"/v1/runs/{run_a}/artifacts/upload", json=body, headers=_auth(write_key))
    second = await api_client.post(f"/v1/runs/{run_b}/artifacts/upload", json=body, headers=_auth(write_key))

    assert first.status_code == HTTPStatus.CREATED
    assert second.status_code == HTTPStatus.CREATED
    assert len(upload_calls) == 1
    assert first.json()["role"] == "produced"
    assert second.json()["role"] == "used"
    assert second.json()["uri"] == first.json()["uri"]


async def test_artifacts_upload_dedup_cannot_override_drive_url(api_client, write_key, monkeypatch):
    """A dedup hit's drive_url must always come from the original upload,
    never a later caller's own meta — otherwise the second (or a
    malicious) call could redirect future downloads anywhere it likes."""
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
    run_a = (await _enqueue_run(api_client, write_key)).json()["id"]
    run_b_resp = await api_client.post(
        "/v1/experiments/cn-7/runs", json={"slug": "seed-1"}, headers=_auth(write_key)
    )
    run_b = run_b_resp.json()["id"]

    first = await api_client.post(
        f"/v1/runs/{run_a}/artifacts/upload",
        json={"filename": "harness.py", "kind": "other", "content_base64": "aGVsbG8=", "name": "harness.py"},
        headers=_auth(write_key),
    )
    second = await api_client.post(
        f"/v1/runs/{run_b}/artifacts/upload",
        json={
            "filename": "harness.py",
            "kind": "other",
            "content_base64": "aGVsbG8=",
            "name": "harness.py",
            "meta": {"drive_url": "https://evil.example.com/phish"},
        },
        headers=_auth(write_key),
    )

    assert second.status_code == HTTPStatus.CREATED
    assert second.json()["meta"]["drive_url"] == first.json()["meta"]["drive_url"]


async def test_artifacts_upload_dedup_against_plain_pointer_with_no_drive_url(
    api_client, write_key, monkeypatch
):
    """A dedup hit against an artifact that was never a Drive upload at all
    (a plain pointer registration with no drive_url in its meta) must not
    fabricate one — the caller's own meta is used as-is in that case."""
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: True))
    content_sha256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"  # sha256("hello")

    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    await api_client.post(
        f"/v1/runs/{run_id}/artifacts",
        json={"kind": "other", "uri": "s3://bucket/x", "sha256": content_sha256, "name": "harness.py"},
        headers=_auth(write_key),
    )

    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload",
        json={"filename": "harness.py", "kind": "other", "content_base64": "aGVsbG8=", "name": "harness.py"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert "drive_url" not in resp.json()["meta"]


def _drive_mocks(monkeypatch):
    from chuk_experiments_server import drive_storage
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: True))
    monkeypatch.setattr(drive_storage, "get_client", lambda: "fake-service")
    monkeypatch.setattr(drive_storage, "ensure_folder", lambda service, name, parent_id: "root-folder-id")
    monkeypatch.setattr(drive_storage, "ensure_folder_path", lambda service, root_id, parts: "leaf-folder-id")
    upload_calls = []
    file_ids = iter(f"fake-file-id-{i}" for i in range(1000))
    monkeypatch.setattr(
        drive_storage,
        "upload_bytes",
        lambda service, filename, content, parent_id: upload_calls.append(filename) or next(file_ids),
    )
    return upload_calls


async def test_artifacts_upload_batch_creates_each_item(api_client, write_key, monkeypatch):
    _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]

    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-batch",
        json={
            "items": [
                {"filename": "a.py", "kind": "other", "content_base64": "aGVsbG8=", "name": "a"},
                {"filename": "b.py", "kind": "other", "content_base64": "d29ybGQ=", "name": "b"},
            ]
        },
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED
    body = resp.json()
    assert [a["name"] for a in body] == ["a", "b"]
    assert [a["role"] for a in body] == ["produced", "produced"]
    assert body[0]["uri"] != body[1]["uri"]


async def test_artifacts_upload_batch_dedups_within_same_batch(api_client, write_key, monkeypatch):
    upload_calls = _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]

    same_item = {"filename": "harness.py", "kind": "other", "content_base64": "aGVsbG8=", "name": "harness"}
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-batch",
        json={"items": [same_item, dict(same_item)]},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED
    body = resp.json()
    assert len(upload_calls) == 1
    assert [a["role"] for a in body] == ["produced", "used"]
    assert body[0]["uri"] == body[1]["uri"]


async def test_artifacts_upload_batch_bad_item_fails_whole_batch_with_no_uploads(
    api_client, write_key, monkeypatch
):
    upload_calls = _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]

    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-batch",
        json={
            "items": [
                {"filename": "good.py", "kind": "other", "content_base64": "aGVsbG8=", "name": "good"},
                {
                    "filename": "bad.py",
                    "kind": "other",
                    "content_base64": "not-valid-base64!!!",
                    "name": "bad",
                },
            ]
        },
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "items[1]" in resp.json()["error"]
    assert upload_calls == []


async def test_artifacts_upload_batch_not_configured(api_client, write_key, monkeypatch):
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: False))
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-batch",
        json={"items": [{"filename": "x.txt", "kind": "other", "content_base64": "aGVsbG8=", "name": "x"}]},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.NOT_IMPLEMENTED


async def test_artifacts_upload_batch_rejects_empty_items(api_client, write_key, monkeypatch):
    _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-batch", json={"items": []}, headers=_auth(write_key)
    )
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_artifacts_upload_rejects_invalid_base64(api_client, write_key, monkeypatch):
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: True))
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload",
        json={"filename": "x.txt", "kind": "other", "content_base64": "not-valid-base64!!!", "name": "x"},
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
        json={"filename": "x.txt", "kind": "other", "content_base64": "aGVsbG8=", "name": "x"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_artifacts_upload_raw_creates_artifact(api_client, write_key, monkeypatch):
    """Multipart upload — the curl -F path — hits the same dedup/register
    core as the JSON routes, just with bytes read straight from the
    uploaded file instead of base64-decoded."""
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
        f"/v1/runs/{run_id}/artifacts/upload-raw",
        files={"file": ("harness.py", b"print('hi')", "text/plain")},
        data={"name": "tok-v12-harness", "kind": "other"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED
    body = resp.json()
    assert body["uri"] == "gdrive://fake-file-id"
    assert body["name"] == "tok-v12-harness"
    assert body["role"] == "produced"
    assert body["meta"]["source_path"] == "harness.py"
    assert "drive_url" in body["meta"]


async def test_artifacts_upload_raw_dedups_by_name_and_sha(api_client, write_key, monkeypatch):
    upload_calls = _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_a = (await _enqueue_run(api_client, write_key)).json()["id"]
    run_b_resp = await api_client.post(
        "/v1/experiments/cn-7/runs", json={"slug": "seed-1"}, headers=_auth(write_key)
    )
    run_b = run_b_resp.json()["id"]

    first = await api_client.post(
        f"/v1/runs/{run_a}/artifacts/upload-raw",
        files={"file": ("harness.py", b"print('hi')", "text/plain")},
        data={"name": "tok-v12-harness"},
        headers=_auth(write_key),
    )
    second = await api_client.post(
        f"/v1/runs/{run_b}/artifacts/upload-raw",
        files={"file": ("harness.py", b"print('hi')", "text/plain")},
        data={"name": "tok-v12-harness"},
        headers=_auth(write_key),
    )

    assert first.status_code == HTTPStatus.CREATED
    assert second.status_code == HTTPStatus.CREATED
    assert len(upload_calls) == 1
    assert first.json()["role"] == "produced"
    assert second.json()["role"] == "used"
    assert second.json()["uri"] == first.json()["uri"]


async def test_artifacts_upload_raw_requires_file(api_client, write_key, monkeypatch):
    _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-raw",
        data={"name": "x"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_artifacts_upload_raw_requires_name(api_client, write_key, monkeypatch):
    _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-raw",
        files={"file": ("x.txt", b"hello", "text/plain")},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_artifacts_upload_raw_rejects_oversized_content(api_client, write_key, monkeypatch):
    from chuk_experiments_server import rest
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: True))
    monkeypatch.setattr(rest, "_MAX_UPLOAD_BYTES", 4)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-raw",
        files={"file": ("x.txt", b"hello world", "text/plain")},
        data={"name": "x"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_artifacts_upload_raw_not_configured(api_client, write_key, monkeypatch):
    from chuk_experiments_server.config import settings

    monkeypatch.setattr(type(settings), "google_drive_configured", property(lambda self: False))
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-raw",
        files={"file": ("x.txt", b"hello", "text/plain")},
        data={"name": "x"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.NOT_IMPLEMENTED


async def test_artifacts_upload_raw_meta_round_trips_but_cannot_override_drive_url(
    api_client, write_key, monkeypatch
):
    _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-raw",
        files={"file": ("x.txt", b"hello", "text/plain")},
        data={"name": "x", "meta": json.dumps({"note": "custom", "drive_url": "https://evil.example.com"})},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.CREATED
    body = resp.json()
    assert body["meta"]["note"] == "custom"
    assert body["meta"]["drive_url"].startswith("https://drive.google.com/")


async def test_artifacts_upload_raw_rejects_invalid_meta_json(api_client, write_key, monkeypatch):
    _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-raw",
        files={"file": ("x.txt", b"hello", "text/plain")},
        data={"name": "x", "meta": "not-json"},
        headers=_auth(write_key),
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


async def test_artifacts_upload_raw_rejects_invalid_role(api_client, write_key, monkeypatch):
    _drive_mocks(monkeypatch)
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    resp = await api_client.post(
        f"/v1/runs/{run_id}/artifacts/upload-raw",
        files={"file": ("x.txt", b"hello", "text/plain")},
        data={"name": "x", "role": "not-a-real-role"},
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


async def test_artifact_download_rejects_untrusted_drive_url(api_client, write_key):
    """register_artifact accepts arbitrary meta, so a crafted drive_url
    pointing off Google's domain must never be followed — otherwise
    /download is an open redirect for any WRITE-scoped caller."""
    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    artifact_id = (
        await api_client.post(
            f"/v1/runs/{run_id}/artifacts",
            json={
                "kind": "other",
                "uri": "gdrive://abc123",
                "meta": {"drive_url": "https://evil.example.com/phish"},
            },
            headers=_auth(write_key),
        )
    ).json()["id"]

    resp = await api_client.get(f"/v1/artifacts/{artifact_id}/download", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.INTERNAL_SERVER_ERROR


async def test_artifact_lineage_splits_produced_and_used(api_client, write_key):
    await _create_experiment(api_client, write_key)
    run_a = (await _enqueue_run(api_client, write_key)).json()["id"]
    run_b_resp = await api_client.post(
        "/v1/experiments/cn-7/runs", json={"slug": "seed-1"}, headers=_auth(write_key)
    )
    run_b = run_b_resp.json()["id"]

    produced = (
        await api_client.post(
            f"/v1/runs/{run_a}/artifacts",
            json={"kind": "other", "uri": "gdrive://abc", "sha256": "deadbeef", "name": "harness"},
            headers=_auth(write_key),
        )
    ).json()
    await api_client.post(
        f"/v1/runs/{run_b}/artifacts",
        json={
            "kind": "other",
            "uri": "gdrive://abc",
            "sha256": "deadbeef",
            "name": "harness",
            "role": "used",
        },
        headers=_auth(write_key),
    )

    resp = await api_client.get(f"/v1/artifacts/{produced['id']}/lineage", headers=_auth(write_key))
    assert resp.status_code == HTTPStatus.OK
    body = resp.json()
    assert body["produced_by_run_id"] == run_a
    assert body["used_by_run_ids"] == [run_b]


async def test_pins_crud_and_write_scope_gating(api_client, write_key):
    await auth_module.upsert_bootstrap_key("readonly:read:readonly-pins-key")

    await _create_experiment(api_client, write_key)
    run_id = (await _enqueue_run(api_client, write_key)).json()["id"]
    artifact_id = (
        await api_client.post(
            f"/v1/runs/{run_id}/artifacts",
            json={"kind": "other", "uri": "gdrive://abc"},
            headers=_auth(write_key),
        )
    ).json()["id"]

    denied = await api_client.put(
        "/v1/pins/harness:latest",
        json={"artifact_id": artifact_id},
        headers=_auth("readonly-pins-key"),
    )
    assert denied.status_code == HTTPStatus.FORBIDDEN

    created = await api_client.put(
        "/v1/pins/harness:latest", json={"artifact_id": artifact_id}, headers=_auth(write_key)
    )
    assert created.status_code == HTTPStatus.OK
    assert created.json()["artifact_id"] == artifact_id

    fetched = await api_client.get("/v1/pins/harness:latest", headers=_auth(write_key))
    assert fetched.status_code == HTTPStatus.OK
    assert fetched.json()["id"] == artifact_id

    listed = await api_client.get("/v1/pins", headers=_auth(write_key))
    assert listed.status_code == HTTPStatus.OK
    pins = listed.json()
    assert [p["name"] for p in pins] == ["harness:latest"]
    assert pins[0]["run_id"] == run_id
    assert pins[0]["uri"] == "gdrive://abc"


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
