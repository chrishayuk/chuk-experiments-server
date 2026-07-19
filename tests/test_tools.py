"""tools.py tests — each tool is called as a plain async function (@mcp.tool
just wraps-through, per chuk_mcp_server's decorator), with `tool_caller`
(see conftest.py) wiring its internal REST forwarding to the in-process ASGI
app and faking the calling agent's bearer token. This exercises the real
MCP-to-REST forwarding path, not service.py directly."""

import httpx

from chuk_experiments_server import internal_client, tools


async def test_get_index_empty(tool_caller):
    assert await tools.get_index() == []


async def test_create_experiment_then_get(tool_caller):
    created = await tools.create_experiment(programme="cn", slug="cn-7", title="t", hypothesis="h")
    assert created["slug"] == "cn-7"

    fetched = await tools.get_experiment("cn-7")
    assert fetched["title"] == "t"


async def test_create_experiment_duplicate_returns_error_dict_not_raise(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    result = await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    assert "error" in result


async def test_list_experiments_filters(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    await tools.create_experiment(programme="div", slug="div-3", title="t2")
    result = await tools.list_experiments(programme="div")
    assert [e["slug"] for e in result] == ["div-3"]


async def test_search_experiments_with_filters_dict(tool_caller):
    await tools.create_experiment(
        programme="cn", slug="cn-7", title="fingerprint stuff", hypothesis="fingerprint"
    )
    result = await tools.search_experiments(query="fingerprint")
    assert [h["slug"] for h in result] == ["cn-7"]


async def test_append_writeup_uses_calling_key_as_author(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    writeup = await tools.append_writeup("cn-7", "# hi")
    assert writeup["author"] == "pytest"


async def test_enqueue_run_and_get_run(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={"entrypoint": "true"})
    assert run["status"] == "queued"

    fetched = await tools.get_run(run["id"])
    assert fetched["experiment_slug"] == "cn-7"


async def test_submit_result_uses_calling_key_as_submitted_by(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})
    result = await tools.submit_result(run["id"], name="acc", value=0.9)
    assert result["submitted_by"] == "pytest"


async def test_register_artifact_and_find_checkpoints(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})
    await tools.register_artifact(run["id"], kind="checkpoint", uri="s3://bucket/ckpt.bin")

    found = await tools.find_checkpoints(kind="checkpoint")
    assert [a["uri"] for a in found] == ["s3://bucket/ckpt.bin"]


async def test_register_artifact_lineage_and_pins(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})

    produced = await tools.register_artifact(
        run["id"], kind="other", uri="gdrive://abc", sha256="deadbeef", name="harness"
    )

    lineage = await tools.get_artifact_lineage(produced["id"])
    assert lineage["produced_by_run_id"] == run["id"]
    assert lineage["used_by_run_ids"] == []

    pin = await tools.set_pin("harness:latest", produced["id"])
    assert pin["artifact_id"] == produced["id"]
    resolved = await tools.get_pin("harness:latest")
    assert resolved["id"] == produced["id"]


async def test_register_git_artifact_builds_uri_and_computed_meta(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})

    artifact = await tools.register_git_artifact(
        run["id"], owner="chrishayuk", repo="chuk-mlx", commit="abc123", name="harness"
    )
    assert artifact["uri"] == "git+https://github.com/chrishayuk/chuk-mlx@abc123"
    assert artifact["meta"]["git_repo"] == "chrishayuk/chuk-mlx"
    assert artifact["meta"]["git_commit"] == "abc123"


async def test_register_git_artifact_computed_meta_wins_over_caller_supplied(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})

    artifact = await tools.register_git_artifact(
        run["id"],
        owner="chrishayuk",
        repo="chuk-mlx",
        commit="abc123",
        meta={"git_repo": "attacker/fake", "git_commit": "evil", "extra": "kept"},
    )
    assert artifact["meta"]["git_repo"] == "chrishayuk/chuk-mlx"
    assert artifact["meta"]["git_commit"] == "abc123"
    assert artifact["meta"]["extra"] == "kept"


async def test_register_hf_artifact_builds_uri_and_computed_meta(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})

    artifact = await tools.register_hf_artifact(
        run["id"],
        repo_id="chrishayuk/granite-4.1-3b-q4k-vindex",
        revision="main",
        repo_type="model",
        kind="checkpoint",
        bytes=4_230_000_000,
    )
    assert artifact["uri"] == "hf://model/chrishayuk/granite-4.1-3b-q4k-vindex@main"
    assert artifact["meta"]["hf_repo_id"] == "chrishayuk/granite-4.1-3b-q4k-vindex"
    assert artifact["meta"]["hf_revision"] == "main"
    assert artifact["meta"]["hf_repo_type"] == "model"
    assert artifact["bytes"] == 4_230_000_000


async def test_verify_artifact_tool_forwards_to_verify_route(tool_caller, monkeypatch):
    from chuk_experiments_server import external_refs

    async def _fake_verify_git_ref(*args, **kwargs):
        return external_refs.VerifyResult("verified", "commit exists")

    monkeypatch.setattr(external_refs, "verify_git_ref", _fake_verify_git_ref)

    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})
    artifact = await tools.register_git_artifact(
        run["id"], owner="chrishayuk", repo="chuk-mlx", commit="abc123"
    )

    result = await tools.verify_artifact(artifact["id"])
    assert result["verify_status"] == "verified"
    assert result["verify_detail"] == "commit exists"


async def test_upload_artifacts_batch_forwards_and_dedups(tool_caller, monkeypatch):
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

    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})

    same_item = {"filename": "harness.py", "kind": "other", "content_base64": "aGVsbG8=", "name": "harness"}
    results = await tools.upload_artifacts_batch(run["id"], [same_item, dict(same_item)])

    assert len(upload_calls) == 1
    assert [a["role"] for a in results] == ["produced", "used"]
    assert results[0]["uri"] == results[1]["uri"]


async def test_compare_runs(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})
    await tools.submit_result(run["id"], name="acc", value=0.5)

    comparison = await tools.compare_runs([run["id"]], "acc")
    assert comparison[0]["value"] == 0.5


async def test_set_run_status(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})
    updated = await tools.set_run_status(run["id"], "completed")
    assert updated["status"] == "completed"


async def test_update_experiment_status(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    updated = await tools.update_experiment_status("cn-7", "running")
    assert updated["status"] == "running"

    retagged = await tools.update_experiment_status("cn-7", "completed", tags=["done"])
    assert retagged["status"] == "completed"
    assert retagged["tags"] == ["done"]


async def test_cancel_run(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})
    cancelled = await tools.cancel_run(run["id"])
    assert cancelled["status"] == "cancelled"


async def test_cancel_run_conflict_returns_error_dict(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})
    await tools.set_run_status(run["id"], "running")
    result = await tools.cancel_run(run["id"])
    assert "error" in result


async def test_peek_queue(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    await tools.enqueue_run(slug="cn-7", workspec={})
    result = await tools.peek_queue()
    assert len(result) == 1


async def test_list_programmes(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    result = await tools.list_programmes()
    assert [p["slug"] for p in result] == ["cn"]


async def test_list_experiments_filters_by_tag(tool_caller, api_client, write_key):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    await tools.create_experiment(programme="cn", slug="cn-8", title="t2")
    await api_client.patch(
        "/v1/experiments/cn-7", json={"tags": ["baseline"]}, headers={"Authorization": f"Bearer {write_key}"}
    )
    result = await tools.list_experiments(tags=["baseline"])
    assert [e["slug"] for e in result] == ["cn-7"]


async def test_search_experiments_filters_by_tag(tool_caller, api_client, write_key):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    await tools.create_experiment(programme="cn", slug="cn-8", title="t2")
    await api_client.patch(
        "/v1/experiments/cn-7", json={"tags": ["baseline"]}, headers={"Authorization": f"Bearer {write_key}"}
    )
    result = await tools.search_experiments(filters={"tags": ["baseline"]})
    assert [h["slug"] for h in result] == ["cn-7"]


async def test_search_experiments_filters_by_config(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    await tools.enqueue_run(slug="cn-7", workspec={"entrypoint": "true"})
    result = await tools.search_experiments(filters={"config_key": "gpu", "config_value": "a100"})
    # No run has config.gpu = a100, so the structured filter legitimately
    # excludes it — this exercises the filter-building branch, not a match.
    assert result == []


async def test_search_experiments_filters_by_metric(tool_caller):
    await tools.create_experiment(programme="cn", slug="cn-7", title="t")
    run = await tools.enqueue_run(slug="cn-7", workspec={})
    await tools.submit_result(run["id"], name="acc", value=0.9)
    result = await tools.search_experiments(filters={"metric": "acc", "metric_op": "gt", "metric_value": 0.5})
    assert [h["slug"] for h in result] == ["cn-7"]


async def test_api_request_transport_failure_returns_error_dict(monkeypatch, tool_caller):
    class _RaisingClient:
        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(internal_client, "get_client", lambda: _RaisingClient())
    result = await tools.list_programmes()
    assert "internal_request_failed" in result["error"]


async def test_api_request_non_json_response_returns_error_dict(monkeypatch, tool_caller):
    class _FakeResponse:
        def json(self):
            raise ValueError("not json")

    class _NonJsonClient:
        async def request(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(internal_client, "get_client", lambda: _NonJsonClient())
    result = await tools.list_programmes()
    assert result == {"error": "internal_response_not_json"}


async def test_no_bearer_token_returns_unauthorized_error(monkeypatch, api_client):
    """Without an ambient bearer token (no MCP context set up), the REST
    layer's own auth check fires — same failure any other unauthenticated
    client would get, no special-casing in tools.py."""
    from chuk_experiments_server import auth, internal_client

    internal_client.set_client(api_client)
    monkeypatch.setattr(auth, "bearer_from_mcp_context", lambda: None)
    try:
        result = await tools.list_programmes()
    finally:
        internal_client.set_client(None)
    assert "error" in result
