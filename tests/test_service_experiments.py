import pytest

from chuk_experiments_server import service
from chuk_experiments_server.constants import ExperimentStatus
from chuk_experiments_server.models import ExperimentCreate, ExperimentUpdate, WriteupCreate


def _experiment_create(**overrides) -> ExperimentCreate:
    defaults = {
        "programme": "cn",
        "slug": "cn-7",
        "title": "Fingerprint embeddings",
        "hypothesis": "Embeddings carry a stable fingerprint across layers",
    }
    return ExperimentCreate(**{**defaults, **overrides})


async def test_create_experiment_implicitly_creates_programme():
    await service.create_experiment(_experiment_create())
    programmes = await service.list_programmes()
    assert [p.slug for p in programmes] == ["cn"]
    assert programmes[0].experiment_count == 1


async def test_get_or_create_programme_humanizes_unseen_slug():
    from chuk_experiments_server.models import ProgrammeCreate

    programme = await service.get_or_create_programme(ProgrammeCreate(slug="state-construction"))
    assert programme.name == "State Construction"


async def test_get_or_create_programme_respects_explicit_name():
    from chuk_experiments_server.models import ProgrammeCreate

    programme = await service.get_or_create_programme(ProgrammeCreate(slug="larql", name="LARQL"))
    assert programme.name == "LARQL"


async def test_create_experiment_duplicate_slug_raises_conflict():
    await service.create_experiment(_experiment_create())
    with pytest.raises(service.ConflictError):
        await service.create_experiment(_experiment_create())


async def test_get_experiment_includes_latest_writeup_and_runs():
    await service.create_experiment(_experiment_create())
    await service.append_writeup("cn-7", "chris", WriteupCreate(body_md="v1"))
    await service.append_writeup("cn-7", "chris", WriteupCreate(body_md="v2"))

    experiment = await service.get_experiment("cn-7")
    assert experiment.latest_writeup.version == 2
    assert experiment.latest_writeup.body_md == "v2"


async def test_get_experiment_missing_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.get_experiment("does-not-exist")


async def test_update_experiment_status_and_tags():
    await service.create_experiment(_experiment_create())
    updated = await service.update_experiment(
        "cn-7", ExperimentUpdate(status=ExperimentStatus.COMPLETED, tags=["v11"])
    )
    assert updated.status == ExperimentStatus.COMPLETED
    assert updated.tags == ["v11"]


async def test_update_experiment_missing_raises_not_found():
    with pytest.raises(service.NotFoundError):
        await service.update_experiment("does-not-exist", ExperimentUpdate(status=ExperimentStatus.COMPLETED))


async def test_update_experiment_conclusion_and_next_action():
    await service.create_experiment(_experiment_create())
    updated = await service.update_experiment(
        "cn-7", ExperimentUpdate(conclusion="Refuted: no effect observed.", next_action="Close out.")
    )
    assert updated.conclusion == "Refuted: no effect observed."
    assert updated.next_action == "Close out."

    # Omitted fields on a later PATCH must not clobber what's already set.
    reloaded = await service.update_experiment("cn-7", ExperimentUpdate(tags=["done"]))
    assert reloaded.conclusion == "Refuted: no effect observed."
    assert reloaded.next_action == "Close out."


async def test_list_experiments_filters_needs_conclusion():
    await service.create_experiment(
        _experiment_create(slug="cn-done-no-conclusion", status=ExperimentStatus.COMPLETED)
    )
    await service.create_experiment(
        _experiment_create(slug="cn-done-with-conclusion", status=ExperimentStatus.COMPLETED)
    )
    await service.update_experiment("cn-done-with-conclusion", ExperimentUpdate(conclusion="Supported."))
    await service.create_experiment(
        _experiment_create(slug="cn-still-running", status=ExperimentStatus.RUNNING)
    )

    needing = await service.list_experiments(needs_conclusion=True)
    assert [e.slug for e in needing] == ["cn-done-no-conclusion"]


async def test_list_experiments_filters_needs_next_action():
    await service.create_experiment(
        _experiment_create(slug="cn-planned-no-action", status=ExperimentStatus.PLANNED)
    )
    await service.create_experiment(
        _experiment_create(slug="cn-running-no-action", status=ExperimentStatus.RUNNING)
    )
    await service.create_experiment(
        _experiment_create(slug="cn-running-with-action", status=ExperimentStatus.RUNNING)
    )
    await service.update_experiment("cn-running-with-action", ExperimentUpdate(next_action="Try TOK-14."))
    await service.create_experiment(
        _experiment_create(slug="cn-done-no-action", status=ExperimentStatus.COMPLETED)
    )

    needing = await service.list_experiments(needs_next_action=True)
    assert sorted(e.slug for e in needing) == ["cn-planned-no-action", "cn-running-no-action"]


async def test_get_research_health_counts():
    await service.create_experiment(
        _experiment_create(slug="cn-done-no-conclusion", status=ExperimentStatus.COMPLETED)
    )
    await service.create_experiment(
        _experiment_create(slug="cn-planned-no-action", status=ExperimentStatus.PLANNED)
    )

    health = await service.get_research_health()
    assert health == {"needs_conclusion": 1, "needs_next_action": 1}


async def test_list_experiments_filters_by_programme_and_status():
    await service.create_experiment(_experiment_create(programme="cn", slug="cn-7"))
    await service.create_experiment(
        _experiment_create(programme="div", slug="div-3", status=ExperimentStatus.RUNNING)
    )

    cn_only = await service.list_experiments(programme="cn")
    assert [e.slug for e in cn_only] == ["cn-7"]

    running_only = await service.list_experiments(status="running")
    assert [e.slug for e in running_only] == ["div-3"]


async def test_list_experiments_filters_by_tag():
    await service.create_experiment(_experiment_create(tags=["v11", "fingerprint"]))
    await service.create_experiment(_experiment_create(slug="cn-8", tags=["v12"]))

    tagged = await service.list_experiments(tags=["fingerprint"])
    assert [e.slug for e in tagged] == ["cn-7"]


async def test_list_experiments_sorts_by_title_ascending():
    await service.create_experiment(_experiment_create(slug="cn-b", title="B experiment"))
    await service.create_experiment(_experiment_create(slug="cn-a", title="A experiment"))

    ascending = await service.list_experiments(sort="title", order="asc")
    assert [e.slug for e in ascending] == ["cn-a", "cn-b"]


async def test_list_experiments_rejects_unknown_sort_column():
    with pytest.raises(service.ValidationError):
        await service.list_experiments(sort="not_a_real_column")


async def test_list_experiments_rejects_invalid_order():
    with pytest.raises(service.ValidationError):
        await service.list_experiments(order="sideways")


async def test_search_experiments_full_text_ranks_relevant_first():
    await service.create_experiment(
        _experiment_create(
            title="Layer-phase readout of fingerprint embeddings", hypothesis="fingerprint signal"
        )
    )
    await service.create_experiment(
        _experiment_create(
            slug="cn-8", title="Unrelated batching experiment", hypothesis="batching throughput"
        )
    )

    hits = await service.search_experiments(query="fingerprint")
    assert [h.slug for h in hits] == ["cn-7"]


async def test_search_experiments_structured_filters_without_query():
    await service.create_experiment(_experiment_create(programme="cn", slug="cn-7"))
    await service.create_experiment(_experiment_create(programme="div", slug="div-3"))

    hits = await service.search_experiments(programme="div")
    assert [h.slug for h in hits] == ["div-3"]


async def test_search_experiments_metric_predicate():
    await service.create_experiment(_experiment_create())
    run = await service.enqueue_run(_run_create())
    from chuk_experiments_server.models import ResultCreate

    await service.submit_result(run.id, "tester", ResultCreate(name="gsm8k_acc", value=0.55))

    above_threshold = await service.search_experiments(metric="gsm8k_acc", metric_op="gt", metric_value=0.4)
    assert [h.slug for h in above_threshold] == ["cn-7"]

    below_threshold = await service.search_experiments(metric="gsm8k_acc", metric_op="gt", metric_value=0.9)
    assert below_threshold == []


async def test_get_index_includes_headline_metric():
    await service.create_experiment(_experiment_create())
    run = await service.enqueue_run(_run_create())
    from chuk_experiments_server.models import ResultCreate

    await service.submit_result(run.id, "tester", ResultCreate(name="gsm8k_acc", value=0.72, verdict="pass"))

    index, total = await service.get_index()
    assert len(index) == 1
    assert total == 1
    assert index[0].headline_metric.name == "gsm8k_acc"
    assert index[0].headline_metric.value == 0.72


async def test_get_index_excludes_superseded_headline_metric():
    await service.create_experiment(_experiment_create())
    run = await service.enqueue_run(_run_create())
    from chuk_experiments_server.models import ResultCreate

    old = await service.submit_result(run.id, "tester", ResultCreate(name="acc", value=0.5))
    await service.submit_result(run.id, "tester", ResultCreate(name="acc", value=0.9, supersedes=old.id))

    index, _total = await service.get_index()
    assert index[0].headline_metric.value == 0.9


async def test_get_index_respects_limit_and_offset_and_reports_total():
    for i in range(3):
        await service.create_experiment(_experiment_create(slug=f"cn-{i}"))

    first_page, total_1 = await service.get_index(limit=2)
    second_page, total_2 = await service.get_index(limit=2, offset=2)
    assert len(first_page) == 2
    assert len(second_page) == 1
    assert total_1 == 3
    assert total_2 == 3


async def test_get_index_filters_by_programme():
    await service.create_experiment(_experiment_create())
    await service.create_experiment(_experiment_create(programme="other", slug="other-1"))

    index, total = await service.get_index(programme="cn")
    assert [e.slug for e in index] == ["cn-7"]
    assert total == 1


async def test_get_index_truncates_long_hypothesis():
    long_hypothesis = "x" * 300
    await service.create_experiment(_experiment_create(hypothesis=long_hypothesis))

    index, _total = await service.get_index()
    assert len(index[0].hypothesis) == 200


def _run_create(**overrides):
    from chuk_experiments_server.models import RunCreate

    defaults = {"experiment": "cn-7", "slug": "seed-0"}
    return RunCreate(**{**defaults, **overrides})
