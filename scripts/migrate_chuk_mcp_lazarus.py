#!/usr/bin/env python3
"""Seed chuk-experiments-server from chuk-mcp-lazarus's ExperimentStore.

ExperimentStore (chuk_mcp_lazarus/experiment_store.py) persists one JSON file
per experiment_id to ~/.chuk-lazarus/experiments/ — {metadata: {name,
model_id, created_at, description, tags}, steps: [{step_name, recorded_at,
data}]}. Read directly off disk (it's just JSON); no need to go through the
running MCP server or import the lazarus package.

Of 1512 files on disk, 1321 (87%) are pytest fixture noise from
test_experiment_store.py / test_experiment_tools.py accumulating over every
test run, never cleaned up: names like "exp1"/"exp2"/"my_exp"/"test_exp",
empty descriptions, placeholder tags ({"a","b"} / {"tag1","tag2"}). Those are
filtered out (see `is_noise`) rather than migrated as if they were research.

The remaining ~191 have real names/descriptions/tags but only 172 unique
names — some experiments were rerun under the same name on different dates.
Reruns map naturally onto this schema's run concept: one experiment per
unique name, one run per underlying lazarus experiment_id (dated slug), each
step becoming a `result` row with its `data` dict as `value_json`.

Talks to a running server over the REST API, same as the other migration
scripts (and reuses their shared helpers).
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from _migrate_common import create_experiment as post_experiment
from _migrate_common import create_run as post_run
from _migrate_common import slugify

_PROGRAMME_SLUG = "lazarus"
_DEFAULT_SOURCE = Path.home() / ".chuk-lazarus" / "experiments"
_NOISE_NAMES = {"test_exp", "my_exp", "exp1", "exp2", "exp3", "exp4", "exp5", "demo", "example", "experiment", "test"}
_NOISE_TAG_SETS = ({"a", "b"}, {"tag1", "tag2"})


@dataclass
class LazarusEntry:
    experiment_id: str
    name: str
    model_id: str
    created_at: str
    description: str
    tags: list[str]
    steps: list[dict] = field(default_factory=list)


def is_noise(entry: LazarusEntry) -> bool:
    tags = set(entry.tags)
    noisy_tags = any(tags <= noise_set for noise_set in _NOISE_TAG_SETS) or not tags
    noisy_name = entry.name.strip().lower() in _NOISE_NAMES
    trivial_desc = entry.description.strip() in ("", "test desc") or len(entry.description.strip()) < 10
    return noisy_tags or noisy_name or (trivial_desc and not entry.steps)


def load_entries(source: Path) -> list[LazarusEntry]:
    entries = []
    for path in source.glob("*.json"):
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        meta = raw.get("metadata", {})
        entries.append(
            LazarusEntry(
                experiment_id=meta.get("experiment_id", path.stem),
                name=(meta.get("name") or "").strip(),
                model_id=meta.get("model_id", ""),
                created_at=meta.get("created_at", ""),
                description=(meta.get("description") or "").strip(),
                tags=list(meta.get("tags", [])),
                steps=raw.get("steps", []),
            )
        )
    return entries


def group_by_name(entries: list[LazarusEntry]) -> dict[str, list[LazarusEntry]]:
    groups: dict[str, list[LazarusEntry]] = defaultdict(list)
    for entry in entries:
        groups[slugify(entry.name)].append(entry)
    for group in groups.values():
        group.sort(key=lambda e: e.created_at)
    return groups


def build_hypothesis(group: list[LazarusEntry]) -> str:
    representative = max(group, key=lambda e: len(e.steps))
    return representative.description


def build_tags(group: list[LazarusEntry]) -> list[str]:
    tags = {_PROGRAMME_SLUG, "historical"}
    for entry in group:
        tags.update(entry.tags)
    return sorted(tags)


def run_migration(source: Path, base_url: str, api_key: str | None, dry_run: bool) -> None:
    entries = load_entries(source)
    real_entries = [e for e in entries if not is_noise(e)]
    groups = group_by_name(real_entries)
    total_steps = sum(len(e.steps) for e in real_entries)
    print(
        f"{len(entries)} files on disk, {len(real_entries)} real "
        f"({len(entries) - len(real_entries)} filtered as test noise), "
        f"{len(groups)} unique experiment names, {total_steps} result steps total"
    )

    if dry_run:
        for slug, group in sorted(groups.items()):
            print(f"  {slug:45s} runs={len(group)} steps={sum(len(e.steps) for e in group)}")
        return

    if not api_key:
        sys.exit("--api-key is required unless --dry-run")

    client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    created_experiments = 0
    created_runs = 0

    for name_slug, group in groups.items():
        slug = f"{_PROGRAMME_SLUG}-{name_slug}"
        representative = max(group, key=lambda e: len(e.steps))

        experiment = post_experiment(
            client,
            {
                "programme": _PROGRAMME_SLUG,
                "slug": slug,
                "title": representative.name,
                "hypothesis": build_hypothesis(group),
                "design": {"model_id": representative.model_id, "lazarus_ids": [e.experiment_id for e in group]},
                "tags": build_tags(group),
                "status": "completed",
            },
        )
        if experiment is None:
            continue
        created_experiments += 1

        for entry in group:
            date_part = entry.created_at[:10] or "undated"
            run = post_run(
                client,
                slug,
                {
                    "slug": f"run-{date_part}-{entry.experiment_id[:8]}",
                    "backend": "other",
                    "config": {"model_id": entry.model_id, "lazarus_experiment_id": entry.experiment_id},
                    "status": "completed",
                },
            )
            if run is None:
                continue
            created_runs += 1

            for step in entry.steps:
                client.post(
                    f"/v1/runs/{run['id']}/results",
                    json={"name": step.get("step_name", "result"), "value_json": step.get("data", {})},
                )

    print(f"Done: created {created_experiments}/{len(groups)} experiments, {created_runs} runs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", help="Bearer key with write scope (required unless --dry-run)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_migration(args.source, args.base_url, args.api_key, args.dry_run)


if __name__ == "__main__":
    main()
