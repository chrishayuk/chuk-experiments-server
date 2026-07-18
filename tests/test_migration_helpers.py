"""Unit tests for the pure parsing/filtering helpers in scripts/migrate_*.py —
no DB, no network. These are the functions most likely to silently misparse
a source file when someone edits chris-experiments/INDEX.md, adds a new
chuk-mlx experiment directory, etc."""

import json

import _migrate_common
import migrate_chris_experiments as mce
import migrate_chuk_mcp_lazarus as mcl
import migrate_chuk_mlx as mcm
import migrate_larql_aim_validation as mlav


def test_slugify_strips_punctuation_and_lowercases():
    assert _migrate_common.slugify("Gemma 3-4B: A/B Test!") == "gemma-3-4b-a-b-test"


def test_slugify_collapses_repeated_separators():
    assert _migrate_common.slugify("a --  b__c") == "a-b-c"


def test_programme_name_override_known_acronym():
    assert _migrate_common.programme_name("larql") == "LARQL"
    assert _migrate_common.programme_name("chuk-mlx") == "CHUK-MLX"


def test_programme_name_override_unknown_slug_returns_none():
    assert _migrate_common.programme_name("shannon") is None


# --- migrate_chris_experiments -----------------------------------------------


def test_programme_slug_and_name_strips_parenthetical_range():
    slug, name, uncommitted = mce.programme_slug_and_name("Foundations (01–10b)")
    assert slug == "foundations"
    assert name == "Foundations"
    assert uncommitted is False


def test_programme_slug_and_name_detects_uncommitted():
    slug, name, uncommitted = mce.programme_slug_and_name("Grammar (00–06)  — uncommitted")
    assert slug == "grammar"
    assert uncommitted is True


def test_experiment_id_and_title_splits_on_em_dash():
    id_part, title = mce.experiment_id_and_title("16 — KnnStore int4 brittleness")
    assert id_part == "16"
    assert title == "KnnStore int4 brittleness"


def test_experiment_id_and_title_no_dash_falls_back_to_whole_heading():
    id_part, title = mce.experiment_id_and_title("Untitled heading with no id")
    assert id_part == ""
    assert title == "Untitled heading with no id"


def test_status_tags_extracts_known_markers():
    assert set(mce.status_tags("done (audit-revised — mid-stack claim retracted)")) == {"retracted"}
    assert set(mce.status_tags("blocked / incomplete")) == {"blocked", "incomplete"}


def test_status_tags_empty_for_clean_status():
    assert mce.status_tags("done") == []


def test_status_tags_none_input():
    assert mce.status_tags(None) == []


def test_map_verdict_superseded_and_abandoned_are_inconclusive():
    assert mce.map_verdict("superseded") == "inconclusive"
    assert mce.map_verdict("abandoned") == "inconclusive"


def test_map_verdict_everything_else_is_pass():
    assert mce.map_verdict("done") == "pass"
    assert mce.map_verdict(None) == "n/a"


def test_build_slug_deduplicates_within_seen_set():
    seen = set()
    first = mce.build_slug("shannon", "61", "Gemma 2 Journey Geometry", seen)
    second = mce.build_slug("shannon", "61", "Gemma 2 Journey Geometry", seen)
    assert first != second
    assert second == f"{first}-2"


def test_parse_index_extracts_experiment_blocks():
    text = """
## Foundations (01–10b)

### 01 — Gate Synthesis
- **Path:** `foundations/01_gate_synthesis/`
- **Status:** superseded
- **Summary:** Attempted heuristic synthesis of FFN gate vectors from triples.
- **Result:** cos~0.01 vs the captured residual — synthesis fails.

---

## Quick filters

**Currently active:** nothing relevant here, no Path bullet so this isn't an experiment.
"""
    experiments = mce.parse_index(text)
    assert len(experiments) == 1
    exp = experiments[0]
    assert exp.programme_slug == "foundations"
    assert exp.path == "foundations/01_gate_synthesis/"
    assert exp.raw_status == "superseded"
    assert "cos~0.01" in exp.result


# --- migrate_chuk_mlx --------------------------------------------------------


def test_humanize_converts_separators_to_title_case():
    assert mcm.humanize("probe_classifier") == "Probe Classifier"
    assert mcm.humanize("routing-wall-breakers") == "Routing Wall Breakers"


def test_extract_title_from_h1_heading():
    text = "# Probe Classifier Experiment\n\nSome body text."
    assert mcm.extract_title(text, "fallback") == "Probe Classifier Experiment"


def test_extract_title_falls_back_when_no_heading():
    assert mcm.extract_title("no heading here", "fallback title") == "fallback title"


def test_extract_takeaway_grabs_paragraph_after_heading():
    text = "## Key Takeaway\n\nDon't force vocabulary alignment.\nThe model already knows the task.\n\n## Next section"
    takeaway = mcm.extract_takeaway(text)
    assert takeaway == "Don't force vocabulary alignment.\nThe model already knows the task."


def test_extract_takeaway_none_when_absent():
    assert mcm.extract_takeaway("# Title\n\nJust prose, no matching heading.") is None


# --- migrate_chuk_mcp_lazarus -------------------------------------------------


def _lazarus_entry(**overrides) -> mcl.LazarusEntry:
    defaults = dict(
        experiment_id="abc123",
        name="real-experiment",
        model_id="google/gemma-3-4b-it",
        created_at="2026-03-26T00:00:00+00:00",
        description="A real research description with real content.",
        tags=["routing", "dark-space"],
        steps=[],
    )
    return mcl.LazarusEntry(**{**defaults, **overrides})


def test_is_noise_flags_placeholder_tag_sets():
    assert mcl.is_noise(_lazarus_entry(tags=["a", "b"]))
    assert mcl.is_noise(_lazarus_entry(tags=["tag1", "tag2"]))
    assert mcl.is_noise(_lazarus_entry(tags=[]))


def test_is_noise_flags_known_test_fixture_names():
    assert mcl.is_noise(_lazarus_entry(name="exp1", tags=["real-tag"]))
    assert mcl.is_noise(_lazarus_entry(name="my_exp", tags=["real-tag"]))


def test_is_noise_flags_trivial_description_with_no_steps():
    assert mcl.is_noise(_lazarus_entry(description="", tags=["real-tag"], steps=[]))


def test_is_noise_false_for_real_entry():
    assert not mcl.is_noise(_lazarus_entry())


def test_is_noise_keeps_trivial_description_if_steps_exist():
    # A short/empty description shouldn't sink an entry that has real step data.
    entry = _lazarus_entry(description="", steps=[{"step_name": "s1", "data": {"acc": 0.9}}])
    assert not mcl.is_noise(entry)


def test_group_by_name_groups_reruns_under_one_slug():
    entries = [
        _lazarus_entry(experiment_id="1", created_at="2026-03-01T00:00:00+00:00"),
        _lazarus_entry(experiment_id="2", created_at="2026-03-02T00:00:00+00:00"),
    ]
    groups = mcl.group_by_name(entries)
    assert list(groups.keys()) == ["real-experiment"]
    assert [e.experiment_id for e in groups["real-experiment"]] == ["1", "2"]  # sorted by created_at


# --- migrate_larql_aim_validation --------------------------------------------


def test_load_valid_artifacts_separates_contract_matches_from_skips(tmp_path):
    valid = {"test_id": "V1", "model": "gemma3-4b-q4k-v2", "metrics": {"kl_divergence": 0.01}}
    (tmp_path / "v1_gemma3-4b.json").write_text(json.dumps(valid))
    (tmp_path / "matrix.json").write_text(json.dumps({"version": 1}))
    (tmp_path / "ad_hoc.json").write_text(json.dumps({"experiment": "FR1", "n": 150}))

    by_test_id, skipped = mlav.load_valid_artifacts(tmp_path)

    assert list(by_test_id.keys()) == ["V1"]
    assert len(by_test_id["V1"]) == 1
    assert [p.name for p in skipped] == ["ad_hoc.json"]


def test_load_valid_artifacts_groups_same_test_id_across_files(tmp_path):
    for model in ("gemma3-4b-q4k-v2", "llama2-7b-q4k"):
        artifact = {"test_id": "V1", "model": model, "metrics": {}}
        (tmp_path / f"v1_{model}.json").write_text(json.dumps(artifact))

    by_test_id, _ = mlav.load_valid_artifacts(tmp_path)
    assert len(by_test_id["V1"]) == 2
