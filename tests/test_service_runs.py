import pydantic
import pytest

from chuk_experiments_server import service
from chuk_experiments_server.constants import RunStatus
from chuk_experiments_server.models import (
    Artifact,
    ArtifactCreate,
    ExperimentCreate,
    ResultCreate,
    RunCreate,
    RunUpdate,
)


async def _make_experiment(slug: str = "cn-7") -> None:
    await service.create_experiment(ExperimentCreate(programme="cn", slug=slug, title="t"))


def test_artifact_verify_status_rejects_values_outside_the_real_literal():
    """verify_status used to be a bare str — nothing tied it to the actual
    closed set external_refs.py produces (verified/missing/unverifiable), so
    a typo'd status would have passed validation silently."""
    base = {"id": 1, "kind": "other", "uri": "s3://x", "created_at": "2026-01-01T00:00:00Z"}
    Artifact.model_validate({**base, "verify_status": "verified"})
    with pytest.raises(pydantic.ValidationError):
        Artifact.model_validate({**base, "verify_status": "not_a_real_status"})


async def test_enqueue_run_missing_experiment_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.enqueue_run(RunCreate(experiment="does-not-exist", slug="seed-0"))


async def test_enqueue_run_duplicate_slug_raises_conflict():
    await _make_experiment()
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    with pytest.raises(service.ConflictError):
        await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))


async def test_get_run_includes_results_and_artifacts():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    await service.submit_result(run.id, "chris", ResultCreate(name="acc", value=0.9))
    await service.register_artifact(
        ArtifactCreate(kind="checkpoint", uri="s3://bucket/ckpt.bin"), run_id=run.id
    )

    fetched = await service.get_run(run.id)
    assert len(fetched.results) == 1
    assert fetched.results[0].name == "acc"
    assert len(fetched.artifacts) == 1
    assert fetched.artifacts[0].uri == "s3://bucket/ckpt.bin"


async def test_get_run_missing_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.get_run("RUN-00000000-000000-absent")


async def test_update_run_status_and_cost():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    updated = await service.update_run(run.id, RunUpdate(status=RunStatus.COMPLETED, cost_usd=1.23))
    assert updated.status == RunStatus.COMPLETED
    assert float(updated.cost_usd) == 1.23


async def test_compare_runs_across_two_experiments():
    await _make_experiment("cn-7")
    await _make_experiment("cn-8")
    run_a = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    run_b = await service.enqueue_run(RunCreate(experiment="cn-8", slug="seed-0"))
    await service.submit_result(run_a.id, "chris", ResultCreate(name="acc", value=0.5))
    await service.submit_result(run_b.id, "chris", ResultCreate(name="acc", value=0.8))

    comparison = await service.compare_runs([run_a.id, run_b.id], "acc")
    values = {row.run_id: row.value for row in comparison}
    found = {row.run_id: row.found for row in comparison}
    assert values[run_a.id] == 0.5
    assert values[run_b.id] == 0.8
    assert found[run_a.id] is True
    assert found[run_b.id] is True


async def test_compare_runs_reports_found_false_when_metric_missing():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    await service.submit_result(run.id, "chris", ResultCreate(name="acc", value=0.5))

    comparison = await service.compare_runs([run.id], "not_a_real_metric")
    assert len(comparison) == 1
    assert comparison[0].found is False
    assert comparison[0].value is None


async def test_compare_runs_excludes_superseded_and_picks_latest():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    old = await service.submit_result(run.id, "chris", ResultCreate(name="bpb", value=0.70))
    new = await service.submit_result(
        run.id, "chris", ResultCreate(name="bpb", value=0.68, supersedes=old.id)
    )

    comparison = await service.compare_runs([run.id], "bpb")
    assert len(comparison) == 1
    assert comparison[0].found is True
    assert comparison[0].value == 0.68

    refetched_old = await service.get_run(run.id)
    old_row = next(r for r in refetched_old.results if r.id == old.id)
    assert old_row.superseded_by == new.id


async def test_mark_result_superseded_rejects_self():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    result = await service.submit_result(run.id, "chris", ResultCreate(name="acc", value=0.5))
    with pytest.raises(service.ValidationError):
        await service.mark_result_superseded(result.id, result.id)


async def test_mark_result_superseded_missing_result_raises_not_found():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    result = await service.submit_result(run.id, "chris", ResultCreate(name="acc", value=0.5))
    with pytest.raises(service.NotFoundError):
        await service.mark_result_superseded(999999999, result.id)


async def test_mark_result_superseded_missing_superseder_raises_not_found():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    result = await service.submit_result(run.id, "chris", ResultCreate(name="acc", value=0.5))
    with pytest.raises(service.NotFoundError):
        await service.mark_result_superseded(result.id, 999999999)


async def test_submit_result_missing_run_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.submit_result(
            "RUN-00000000-000000-absent", "chris", ResultCreate(name="acc", value=1.0)
        )


async def test_register_artifact_missing_run_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.register_artifact(
            ArtifactCreate(kind="checkpoint", uri="s3://x"), run_id="RUN-00000000-000000-absent"
        )


async def test_register_artifact_requires_exactly_one_parent():
    with pytest.raises(service.ValidationError):
        await service.register_artifact(ArtifactCreate(kind="other", uri="s3://x"))


async def test_register_artifact_rejects_both_parents():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    with pytest.raises(service.ValidationError):
        await service.register_artifact(
            ArtifactCreate(kind="other", uri="s3://x"), run_id=run.id, experiment_slug="cn-7"
        )


async def test_register_artifact_against_experiment_directly():
    await _make_experiment()
    artifact = await service.register_artifact(
        ArtifactCreate(kind="other", uri="s3://prereg.json", name="prereg"), experiment_slug="cn-7"
    )
    assert artifact.run_id is None
    assert artifact.experiment_id is not None

    experiment = await service.get_experiment("cn-7")
    assert [a.id for a in experiment.artifacts] == [artifact.id]


async def test_register_artifact_missing_experiment_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.register_artifact(
            ArtifactCreate(kind="other", uri="s3://x"), experiment_slug="does-not-exist"
        )


async def test_register_artifact_rejects_file_uri():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    with pytest.raises(service.ValidationError):
        await service.register_artifact(ArtifactCreate(kind="other", uri="file:///tmp/x.txt"), run_id=run.id)


async def test_register_artifact_rejects_bare_local_path():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    with pytest.raises(service.ValidationError):
        await service.register_artifact(ArtifactCreate(kind="other", uri="/tmp/x.txt"), run_id=run.id)


async def test_register_artifact_accepts_gdrive_and_https_uris():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    gdrive = await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://abc123"), run_id=run.id
    )
    https = await service.register_artifact(
        ArtifactCreate(kind="other", uri="https://example.com/x"), run_id=run.id
    )
    assert gdrive.uri == "gdrive://abc123"
    assert https.uri == "https://example.com/x"


async def test_find_artifact_by_name_sha_returns_none_when_no_match():
    assert await service.find_artifact_by_name_sha("no-such-name", "no-such-sha") is None


async def test_find_artifact_by_name_sha_finds_matching_artifact():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    registered = await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://abc", sha256="deadbeef", name="harness"),
        run_id=run.id,
    )
    found = await service.find_artifact_by_name_sha("harness", "deadbeef")
    assert found is not None
    assert found.id == registered.id


async def test_get_artifact_lineage_splits_produced_and_used():
    await _make_experiment()
    run_a = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    run_b = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-1"))
    produced = await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://abc", sha256="deadbeef", name="harness"),
        run_id=run_a.id,
    )
    await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://abc", sha256="deadbeef", name="harness", role="used"),
        run_id=run_b.id,
    )

    lineage = await service.get_artifact_lineage(produced.id)
    assert lineage.produced_by_run_id == run_a.id
    assert lineage.used_by_run_ids == [run_b.id]


async def test_register_artifact_produced_race_falls_back_to_used():
    """Simulates the dedup race directly: two calls both requesting
    role=produced for the identical (name, sha256), as if two concurrent
    uploads both missed the dedup hit (rest/'s find_artifact_by_name_sha
    check-then-register isn't atomic). The second insert must hit
    idx_artifact_produced_name_sha_unique and gracefully fall back to
    role=used instead of raising — otherwise get_artifact_lineage would
    silently drop whichever run lost the race."""
    await _make_experiment()
    run_a = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    run_b = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-1"))

    first = await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://a", sha256="deadbeef", name="harness", role="produced"),
        run_id=run_a.id,
    )
    second = await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://b", sha256="deadbeef", name="harness", role="produced"),
        run_id=run_b.id,
    )

    assert first.role == "produced"
    assert second.role == "used"

    lineage = await service.get_artifact_lineage(first.id)
    assert lineage.produced_by_run_id == run_a.id
    assert lineage.used_by_run_ids == [run_b.id]


async def test_get_artifact_lineage_empty_for_unnamed_artifact():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    artifact = await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://abc"), run_id=run.id
    )
    lineage = await service.get_artifact_lineage(artifact.id)
    assert lineage.produced_by_run_id is None
    assert lineage.used_by_run_ids == []


async def test_register_git_artifact_dedups_by_name_and_uri_when_no_sha256():
    """git+/hf:// reference artifacts never carry a sha256 — the commit/
    revision in the uri itself is the content address, so dedup and
    lineage must key on (name, uri) instead of (name, sha256) for these."""
    await _make_experiment()
    run_a = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    run_b = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-1"))
    git_uri = "git+https://github.com/chrishayuk/chuk-mlx@abc123"

    produced = await service.register_artifact(
        ArtifactCreate(kind="other", uri=git_uri, name="harness", role="produced"), run_id=run_a.id
    )
    used = await service.register_artifact(
        ArtifactCreate(kind="other", uri=git_uri, name="harness", role="produced"), run_id=run_b.id
    )

    assert produced.role == "produced"
    assert used.role == "used"

    lineage = await service.get_artifact_lineage(produced.id)
    assert lineage.produced_by_run_id == run_a.id
    assert lineage.used_by_run_ids == [run_b.id]


async def test_register_git_artifact_different_uri_same_name_both_produced():
    """Different commits under the same name are genuinely different
    content — the (name, uri) dedup key must not conflate them."""
    await _make_experiment()
    run_a = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    run_b = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-1"))

    first = await service.register_artifact(
        ArtifactCreate(
            kind="other", uri="git+https://github.com/chrishayuk/chuk-mlx@commit1", name="harness"
        ),
        run_id=run_a.id,
    )
    second = await service.register_artifact(
        ArtifactCreate(
            kind="other", uri="git+https://github.com/chrishayuk/chuk-mlx@commit2", name="harness"
        ),
        run_id=run_b.id,
    )

    assert first.role == "produced"
    assert second.role == "produced"


async def test_service_register_git_artifact_builds_uri_and_meta():
    """The URI-building/meta-override logic used to live only in tools.py —
    a REST-only caller (or any direct service/ caller) had no way to
    register a git artifact without hand-building the uri. Now it's a real
    service function."""
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    artifact = await service.register_git_artifact(
        "chrishayuk", "chuk-mlx", "abc123", name="harness", run_id=run.id
    )
    assert artifact.uri == "git+https://github.com/chrishayuk/chuk-mlx@abc123"
    assert artifact.meta["git_repo"] == "chrishayuk/chuk-mlx"
    assert artifact.meta["git_commit"] == "abc123"
    assert artifact.run_id == run.id


async def test_service_register_git_artifact_computed_meta_wins():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    artifact = await service.register_git_artifact(
        "chrishayuk",
        "chuk-mlx",
        "abc123",
        run_id=run.id,
        meta={"git_repo": "attacker/fake", "git_commit": "evil", "extra": "kept"},
    )
    assert artifact.meta["git_repo"] == "chrishayuk/chuk-mlx"
    assert artifact.meta["git_commit"] == "abc123"
    assert artifact.meta["extra"] == "kept"


async def test_service_register_git_artifact_against_experiment_slug():
    await _make_experiment()
    artifact = await service.register_git_artifact(
        "chrishayuk", "chuk-mlx", "abc123", name="prereg", experiment_slug="cn-7"
    )
    assert artifact.run_id is None
    assert artifact.experiment_id is not None


async def test_service_register_hf_artifact_builds_uri_and_meta():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    artifact = await service.register_hf_artifact(
        "chrishayuk/granite-4.1-3b-q4k-vindex",
        run_id=run.id,
        revision="main",
        repo_type="model",
        kind="checkpoint",
        bytes=4_230_000_000,
    )
    assert artifact.uri == "hf://model/chrishayuk/granite-4.1-3b-q4k-vindex@main"
    assert artifact.meta["hf_repo_id"] == "chrishayuk/granite-4.1-3b-q4k-vindex"
    assert artifact.meta["hf_revision"] == "main"
    assert artifact.meta["hf_repo_type"] == "model"
    assert artifact.bytes == 4_230_000_000


async def test_pin_set_get_list_and_repoint():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    artifact_a = await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://a"), run_id=run.id
    )
    artifact_b = await service.register_artifact(
        ArtifactCreate(kind="other", uri="gdrive://b"), run_id=run.id
    )

    await service.set_pin("harness:latest", artifact_a.id)
    resolved = await service.get_pin("harness:latest")
    assert resolved.id == artifact_a.id
    pins = await service.list_pins()
    assert [p.name for p in pins] == ["harness:latest"]
    assert pins[0].run_id == run.id
    assert pins[0].uri == "gdrive://a"
    assert pins[0].kind == "other"

    await service.set_pin("harness:latest", artifact_b.id)
    resolved_again = await service.get_pin("harness:latest")
    assert resolved_again.id == artifact_b.id
    assert (await service.list_pins())[0].uri == "gdrive://b"


async def test_set_pin_missing_artifact_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.set_pin("harness:latest", 999999)


async def test_get_pin_missing_name_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.get_pin("no-such-pin")


async def test_get_artifact_returns_registered_artifact():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    registered = await service.register_artifact(
        ArtifactCreate(kind="checkpoint", uri="s3://bucket/ckpt.bin"), run_id=run.id
    )

    fetched = await service.get_artifact(registered.id)
    assert fetched.uri == "s3://bucket/ckpt.bin"
    assert fetched.run_id == run.id


async def test_get_artifact_missing_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.get_artifact(999999)


async def test_find_checkpoints_filters_by_model_and_kind():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0", config={"model": "v11"}))
    await service.register_artifact(
        ArtifactCreate(kind="checkpoint", uri="s3://bucket/ckpt.bin"), run_id=run.id
    )
    await service.register_artifact(ArtifactCreate(kind="log", uri="s3://bucket/train.log"), run_id=run.id)

    checkpoints = await service.find_checkpoints(model="v11", kind="checkpoint")
    assert [a.uri for a in checkpoints] == ["s3://bucket/ckpt.bin"]

    wrong_model = await service.find_checkpoints(model="v12")
    assert wrong_model == []


async def test_verify_artifact_prefers_requesting_users_own_token(monkeypatch):
    """verify_artifact must resolve the *requesting user's* stored token,
    not just whatever settings.github_token happens to be — a shared
    server-wide token defeats the whole point of per-user tokens."""
    from cryptography.fernet import Fernet

    from chuk_experiments_server import external_refs
    from chuk_experiments_server.config import settings
    from chuk_experiments_server.constants import Scope, TokenProvider
    from chuk_experiments_server.models import DashboardIdentity

    fixed_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(type(settings), "token_encryption_key", property(lambda self: fixed_key))
    monkeypatch.setattr(type(settings), "github_token", property(lambda self: "server-wide-token"))

    user = await service.create_user("verifyuser@example.com", Scope.WRITE)
    identity = DashboardIdentity(email=user.email, role=user.role, user_id=user.id)
    await service.set_user_token(identity, TokenProvider.GITHUB, "personal-token")

    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    artifact = await service.register_artifact(
        ArtifactCreate(kind="other", uri="git+https://github.com/chrishayuk/chuk-mlx@abc123"), run_id=run.id
    )

    seen_tokens = []

    async def _fake_verify_git_ref(host, owner, repo, commit, token=None):
        seen_tokens.append(token)
        return external_refs.VerifyResult("verified", "ok")

    monkeypatch.setattr(external_refs, "verify_git_ref", _fake_verify_git_ref)

    await service.verify_artifact(artifact.id, requesting_user_id=user.id)
    assert seen_tokens == ["personal-token"]

    seen_tokens.clear()
    await service.verify_artifact(artifact.id, requesting_user_id=None)
    assert seen_tokens == ["server-wide-token"]


async def test_list_external_ref_artifacts_only_includes_git_and_hf():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    git_artifact = await service.register_artifact(
        ArtifactCreate(kind="other", uri="git+https://github.com/chrishayuk/chuk-mlx@abc123"), run_id=run.id
    )
    hf_artifact = await service.register_artifact(
        ArtifactCreate(kind="checkpoint", uri="hf://model/chrishayuk/some-model@main"), run_id=run.id
    )
    await service.register_artifact(
        ArtifactCreate(kind="checkpoint", uri="s3://bucket/ckpt.bin"), run_id=run.id
    )
    await service.register_artifact(ArtifactCreate(kind="other", uri="gdrive://abc"), run_id=run.id)

    refs = await service.list_external_ref_artifacts()
    assert {r.id for r in refs} == {git_artifact.id, hf_artifact.id}
    assert all(r.experiment_slug == "cn-7" for r in refs)


async def test_list_external_ref_artifacts_respects_limit_and_offset():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    for i in range(3):
        await service.register_artifact(
            ArtifactCreate(kind="other", uri=f"git+https://github.com/chrishayuk/chuk-mlx@commit{i}"),
            run_id=run.id,
        )

    page = await service.list_external_ref_artifacts(limit=2, offset=0)
    assert len(page) == 2

    rest = await service.list_external_ref_artifacts(limit=2, offset=2)
    assert len(rest) == 1


async def test_list_external_ref_artifacts_empty_when_none_registered():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0"))
    await service.register_artifact(
        ArtifactCreate(kind="checkpoint", uri="s3://bucket/ckpt.bin"), run_id=run.id
    )
    assert await service.list_external_ref_artifacts() == []
