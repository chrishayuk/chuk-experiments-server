#!/usr/bin/env python3
"""Seed chuk-experiments-server from larql/bench/aim-validation/.

chris-source/larql is a production Rust codebase, not a research log —
docs/findings.md, ROADMAP.md, ROADMAP_STATUS.md, and the SESSION_*.md notes
are prose status/design documents that don't decompose into discrete
experiments, and this script deliberately does NOT migrate them (forcing
prose into experiment rows would fabricate structure that isn't there).

bench/aim-validation/ is different: it's a small documented benchmark
harness (see its own README's "Result Contract") with test_id/model/metrics
JSON artifacts for a V1-V4 validation matrix. Of the ~16 files there, only
the ones matching that contract are migrated here — the rest (fr1/fr2/fr3_*,
ave_*, v2_*_scan) are ad-hoc one-off JSON shapes with no shared contract, and
are listed as skipped rather than force-parsed.

Multiple files can share a test_id (the same validation run across several
models) — that's this schema's run concept: one experiment per test_id, one
run per file (slug = model id).

Talks to a running server over the REST API, same as the other migration
scripts.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import httpx

from _migrate_common import create_experiment as post_experiment
from _migrate_common import create_run as post_run

_PROGRAMME_SLUG = "larql"
_DEFAULT_SOURCE = Path("../larql/bench/aim-validation")
_SKIP_FILENAMES = {"matrix.json"}

# From the harness README's Result Contract + each test_id's metric keys —
# there's no per-test_id title in the source, so these are written by hand.
_TITLES = {
    "V1": "AIM Validation V1 — Hash/Top-k Routing Divergence",
    "V1-moe-within-expert": "AIM Validation V1 (MoE within-expert routing)",
    "V2": "AIM Validation V2 — Compression Scan",
    "V3": "AIM Validation V3 — Disk/Paging Behavior",
    "V4": "AIM Validation V4 — Stacked Speedup",
}


def load_valid_artifacts(source: Path) -> tuple[dict[str, list[tuple[Path, dict]]], list[Path]]:
    """Returns (test_id -> [(path, artifact)]), skipped_paths."""
    by_test_id: dict[str, list[tuple[Path, dict]]] = defaultdict(list)
    skipped = []
    for path in sorted(source.glob("*.json")):
        if path.name in _SKIP_FILENAMES:
            continue
        try:
            artifact = json.loads(path.read_text())
        except (OSError, ValueError):
            skipped.append(path)
            continue
        if not isinstance(artifact, dict) or not {"test_id", "model", "metrics"} <= artifact.keys():
            skipped.append(path)
            continue
        by_test_id[artifact["test_id"]].append((path, artifact))
    return by_test_id, skipped


def run_migration(source: Path, base_url: str, api_key: str | None, dry_run: bool) -> None:
    by_test_id, skipped = load_valid_artifacts(source)
    print(f"{sum(len(v) for v in by_test_id.values())} valid artifacts across {len(by_test_id)} test_ids")
    print(f"{len(skipped)} skipped (no shared contract): {[p.name for p in skipped]}")

    if dry_run:
        for test_id, artifacts in by_test_id.items():
            print(f"  {test_id:25s} runs={[a['model'] for _, a in artifacts]}")
        return

    if not api_key:
        sys.exit("--api-key is required unless --dry-run")

    client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    created_experiments = 0
    created_runs = 0

    for test_id, artifacts in by_test_id.items():
        slug = f"{_PROGRAMME_SLUG}-aim-{test_id.lower().replace(' ', '-')}"
        experiment = post_experiment(
            client,
            {
                "programme": _PROGRAMME_SLUG,
                "slug": slug,
                "title": _TITLES.get(test_id, f"AIM Validation {test_id}"),
                "design": {"path": "bench/aim-validation", "test_id": test_id},
                "tags": [_PROGRAMME_SLUG, "historical", "aim-validation"],
                "status": "completed",
            },
        )
        if experiment is None:
            continue
        created_experiments += 1

        for path, artifact in artifacts:
            run = post_run(
                client,
                slug,
                {
                    "slug": artifact["model"],
                    "backend": "other",
                    "config": {
                        "model": artifact["model"],
                        "prompt_set": artifact.get("prompt_set"),
                        "git_rev": artifact.get("git_rev"),
                        "source_file": path.name,
                    },
                    "status": "completed",
                },
            )
            if run is None:
                continue
            created_runs += 1
            client.post(
                f"/v1/runs/{run['id']}/results",
                json={"name": "metrics", "value_json": artifact["metrics"], "notes": artifact.get("notes")},
            )

    print(f"Done: created {created_experiments}/{len(by_test_id)} experiments, {created_runs} runs")


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
