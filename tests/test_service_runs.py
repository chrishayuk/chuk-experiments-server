import pytest

from chuk_experiments_server import service
from chuk_experiments_server.constants import RunStatus
from chuk_experiments_server.models import ArtifactCreate, ExperimentCreate, ResultCreate, RunCreate, RunUpdate


async def _make_experiment(slug: str = "cn-7") -> None:
    await service.create_experiment(ExperimentCreate(programme="cn", slug=slug, title="t"))


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
    await service.register_artifact(run.id, ArtifactCreate(kind="checkpoint", uri="s3://bucket/ckpt.bin"))

    fetched = await service.get_run(run.id)
    assert len(fetched.results) == 1
    assert fetched.results[0].name == "acc"
    assert len(fetched.artifacts) == 1
    assert fetched.artifacts[0].uri == "s3://bucket/ckpt.bin"


async def test_get_run_missing_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.get_run(999999)


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
    assert values[run_a.id] == 0.5
    assert values[run_b.id] == 0.8


async def test_submit_result_missing_run_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.submit_result(999999, "chris", ResultCreate(name="acc", value=1.0))


async def test_register_artifact_missing_run_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.register_artifact(999999, ArtifactCreate(kind="checkpoint", uri="s3://x"))


async def test_find_checkpoints_filters_by_model_and_kind():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="seed-0", config={"model": "v11"}))
    await service.register_artifact(run.id, ArtifactCreate(kind="checkpoint", uri="s3://bucket/ckpt.bin"))
    await service.register_artifact(run.id, ArtifactCreate(kind="log", uri="s3://bucket/train.log"))

    checkpoints = await service.find_checkpoints(model="v11", kind="checkpoint")
    assert [a.uri for a in checkpoints] == ["s3://bucket/ckpt.bin"]

    wrong_model = await service.find_checkpoints(model="v12")
    assert wrong_model == []
