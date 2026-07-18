import asyncio

import pytest

from chuk_experiments_server import service
from chuk_experiments_server.constants import RunStatus
from chuk_experiments_server.models import ExperimentCreate, RunCreate, RunUpdate


async def _make_experiment(slug: str = "cn-7") -> None:
    await service.create_experiment(ExperimentCreate(programme="cn", slug=slug, title="t"))


async def test_peek_queue_excludes_non_queued_runs():
    await _make_experiment()
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="a", status=RunStatus.QUEUED))
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="b", status=RunStatus.COMPLETED))

    ready = await service.peek_queue()
    assert [r.slug for r in ready] == ["a"]


async def test_peek_queue_orders_by_priority_then_age():
    await _make_experiment()
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="low", priority=0))
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="high", priority=10))

    ready = await service.peek_queue()
    assert [r.slug for r in ready] == ["high", "low"]


async def test_peek_queue_filters_by_backend_requirement():
    await _make_experiment()
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="colab-only", requirements={"backend": "colab"}))
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="any-backend", requirements={}))

    for_colab = await service.peek_queue(backend="colab")
    assert {r.slug for r in for_colab} == {"colab-only", "any-backend"}

    for_vastai = await service.peek_queue(backend="vastai")
    assert [r.slug for r in for_vastai] == ["any-backend"]


async def test_peek_queue_respects_max_seconds():
    await _make_experiment()
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="short", est_seconds=100))
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="long", est_seconds=10_000))

    ready = await service.peek_queue(max_seconds=500)
    assert [r.slug for r in ready] == ["short"]


async def test_depends_on_gates_readiness_until_dependency_completes():
    await _make_experiment()
    dependency = await service.enqueue_run(RunCreate(experiment="cn-7", slug="dependency"))
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="dependent", depends_on=[dependency.id]))

    ready_before = await service.peek_queue()
    assert [r.slug for r in ready_before] == ["dependency"]

    await service.update_run(dependency.id, RunUpdate(status=RunStatus.COMPLETED))

    ready_after = await service.peek_queue()
    assert [r.slug for r in ready_after] == ["dependent"]


async def test_claim_queue_marks_claimed_with_lease():
    await _make_experiment()
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="a"))

    claimed = await service.claim_queue(backend="colab", session_seconds=600, claimed_by="worker-1")
    assert len(claimed) == 1
    assert claimed[0].status == RunStatus.CLAIMED
    assert claimed[0].claimed_by == "worker-1"
    assert claimed[0].lease_expires_at is not None

    assert await service.peek_queue() == []


async def test_claim_queue_packs_by_priority_within_budget():
    await _make_experiment()
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="big", priority=10, est_seconds=500))
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="small", priority=5, est_seconds=100))

    # Budget only fits "small" (500 > 300 remaining after nothing claimed yet,
    # but big itself exceeds the whole session budget).
    claimed = await service.claim_queue(backend="any", session_seconds=300, claimed_by="worker-1")
    assert [r.slug for r in claimed] == ["small"]


async def test_claim_queue_never_double_claims_concurrently():
    await _make_experiment()
    await service.enqueue_run(RunCreate(experiment="cn-7", slug="only-one"))

    results = await asyncio.gather(
        service.claim_queue(backend="any", session_seconds=600, claimed_by="worker-1"),
        service.claim_queue(backend="any", session_seconds=600, claimed_by="worker-2"),
    )
    claimed_slugs = [r.slug for batch in results for r in batch]
    assert claimed_slugs == ["only-one"]  # exactly one worker got it, not both


async def test_renew_lease_transitions_claimed_to_running():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="a"))
    claimed = await service.claim_queue(backend="any", session_seconds=600, claimed_by="worker-1")
    assert claimed[0].status == RunStatus.CLAIMED

    renewed = await service.renew_lease(run.id)
    assert renewed.status == RunStatus.RUNNING


async def test_renew_lease_on_queued_run_raises_conflict():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="a"))
    with pytest.raises(service.ConflictError):
        await service.renew_lease(run.id)


async def test_cancel_run_from_queued():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="a"))
    cancelled = await service.cancel_run(run.id)
    assert cancelled.status == RunStatus.CANCELLED


async def test_cancel_run_from_running_raises_conflict():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="a", status=RunStatus.RUNNING))
    with pytest.raises(service.ConflictError):
        await service.cancel_run(run.id)


async def test_sweep_requeues_expired_lease_below_max_attempts():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="a"))
    await service.claim_queue(backend="any", session_seconds=600, claimed_by="worker-1", lease_seconds=-1)

    result = await service.sweep_expired_leases(max_attempts=3)
    assert result.requeued == 1
    assert result.lost == 0

    refetched = await service.get_run(run.id)
    assert refetched.status == RunStatus.QUEUED
    assert refetched.claim_attempts == 1


async def test_sweep_marks_lost_after_max_attempts():
    await _make_experiment()
    run = await service.enqueue_run(RunCreate(experiment="cn-7", slug="a"))
    from chuk_experiments_server.db import get_pool

    pool = await get_pool()
    await pool.execute(
        "UPDATE run SET status = 'claimed', claim_attempts = 2, lease_expires_at = now() - interval '1 minute' "
        "WHERE id = $1",
        run.id,
    )

    result = await service.sweep_expired_leases(max_attempts=3)
    assert result.requeued == 0
    assert result.lost == 1

    refetched = await service.get_run(run.id)
    assert refetched.status == RunStatus.LOST
