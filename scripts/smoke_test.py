#!/usr/bin/env python3
"""Post-deploy schema-drift smoke test — read-only, runs against real
production data right after every deploy to catch exactly the class of bug
that took get_experiment down on 2026-07-20: migration 011 shipped in the
same push as the code that depends on it, but `fly deploy` only restarts
the container, it doesn't run `migrate` — so the new `artifact.experiment_id`
column didn't exist yet, and every single `GET /v1/experiments/{slug}` /
`get_experiment` call 500'd until someone happened to notice and someone
ran `migrate` by hand.

Deliberately not a copy of verify_harness_contract.py's full workflow
validation (that creates a whole test experiment/runs, is slow, and isn't
about schema): this touches only GET routes, against whatever real data
already exists, chosen specifically to reach every column added by a
migration since 006 — the exact things a forgotten `migrate` would leave
missing:
  - 007 (per-user tokens):        not reachable via a bearer key (dashboard-
                                   only, require_dashboard_role) — out of scope
  - 008 (artifact uri dedup):     GET /v1/artifacts/external-refs
  - 009 (conclusion/next_action): GET /v1/experiments/{slug}
  - 010 (result.superseded_by):   GET /v1/runs/{run_id}, GET /v1/runs/compare
  - 011 (artifact.experiment_id): GET /v1/experiments/{slug}  <- what broke

No writes, no side effects, safe to run against production on every deploy.
"""

import argparse
import sys

import httpx

_MIN_EXPECTED_EXPERIMENTS = 1


def _step(name: str) -> None:
    print(f"\n=== {name} ===")


def _check(condition: bool, message: str) -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {message}")
    if not condition:
        raise SystemExit(1)


def run_smoke_test(base_url: str, api_key: str) -> None:
    client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)

    _step("Health")
    health = client.get("/v1/experiments/health")
    _check(health.status_code == 200, f"GET /v1/experiments/health -> {health.status_code}")

    _step("Index (experiment + programme + result join)")
    index = client.get("/v1/index", params={"limit": 5})
    _check(index.status_code == 200, f"GET /v1/index -> {index.status_code}: {index.text[:200]}")
    results = index.json()["results"]
    _check(
        len(results) >= _MIN_EXPECTED_EXPERIMENTS,
        f"index returned {len(results)} experiment(s) to check against",
    )
    slug = results[0]["slug"]

    _step("Experiment detail (migration 009 + 011 columns)")
    detail = client.get(f"/v1/experiments/{slug}")
    _check(
        detail.status_code == 200,
        f"GET /v1/experiments/{slug} -> {detail.status_code}: {detail.text[:300]}",
    )
    experiment = detail.json()
    _check("conclusion" in experiment and "next_action" in experiment, "conclusion/next_action present (009)")
    _check("artifacts" in experiment, "experiment-level artifacts list present (011)")

    run_id = experiment["runs"][0]["id"] if experiment.get("runs") else None
    if run_id is not None:
        _step("Run detail (migration 010 columns)")
        run_detail = client.get(f"/v1/runs/{run_id}")
        _check(
            run_detail.status_code == 200,
            f"GET /v1/runs/{run_id} -> {run_detail.status_code}: {run_detail.text[:300]}",
        )
        run_results = run_detail.json().get("results", [])
        _check(
            all("superseded_by" in r for r in run_results),
            "superseded_by present on every result row (010)",
        )

        _step("Compare runs (migration 010's superseded_by-aware join)")
        compare = client.get("/v1/runs/compare", params={"ids": [run_id], "metric": "__smoke_test_probe__"})
        _check(
            compare.status_code == 200,
            f"GET /v1/runs/compare -> {compare.status_code}: {compare.text[:300]}",
        )
        rows = compare.json()
        _check(
            len(rows) == 1 and rows[0]["found"] is False,
            "compare_runs returns found=false for a metric that doesn't exist, not an error",
        )
    else:
        print(f"  (skipped — {slug} has no runs to check)")

    _step("External refs (migration 008's dedup index + verify columns)")
    refs = client.get("/v1/artifacts/external-refs", params={"limit": 5})
    _check(
        refs.status_code == 200, f"GET /v1/artifacts/external-refs -> {refs.status_code}: {refs.text[:300]}"
    )

    _step("Pins (artifact_pin join)")
    pins = client.get("/v1/pins")
    _check(pins.status_code == 200, f"GET /v1/pins -> {pins.status_code}: {pins.text[:300]}")

    print("\nAll smoke-test checks passed — schema matches deployed code.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="https://chuk-experiments-server.fly.dev")
    parser.add_argument("--api-key", required=True, help="Bearer key with read scope")
    args = parser.parse_args()
    try:
        run_smoke_test(args.base_url, args.api_key)
    except httpx.HTTPError as exc:
        sys.exit(f"transport error talking to {args.base_url}: {exc}")


if __name__ == "__main__":
    main()
