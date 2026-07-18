#!/usr/bin/env python3
"""End-to-end validation of the spec §6/§6a harness contract against a
running chuk-experiments-server — not a pytest test, a script that exercises
the deployed server over real HTTP the way an actual training-harness worker
would, matching the spec's own acceptance bar:

    "register run -> worker claims via /queue/claim -> trains -> checkpoint
    to R2 -> results submitted -> lease expiry tested by killing the runtime
    mid-run and watching the run return to queued."

Covers: enqueue with workspec/requirements/priority/est_seconds, depends_on
gating, atomic claim + packing, lease renewal (claimed -> running), a
presigned checkpoint upload/download round-trip (or a graceful skip if R2
isn't configured on the target server), submitting a result and marking a
run completed, and the lease-expiry sweep — including the multi-attempt path
to 'lost', simulated with a 1-second lease rather than waiting out the real
default (spec's "killing the runtime mid-run" without an actual multi-minute
wait).

Leaves one reusable test experiment behind (slug fixed, tagged "e2e-test")
so reruns add fresh runs under it rather than accumulating duplicate
experiments — this is a repeatable smoke test, not a one-shot migration.
"""

import argparse
import sys
import time
from datetime import datetime, timezone

import httpx

_PROGRAMME = "e2e-harness-test"
_EXPERIMENT_SLUG = "e2e-harness-test-run"
_MAX_CLAIM_ATTEMPTS = 3  # must match constants.DEFAULT_MAX_CLAIM_ATTEMPTS on the target server


def _run_slug(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"


def _step(name: str) -> None:
    print(f"\n=== {name} ===")


def _check(condition: bool, message: str) -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {message}")
    if not condition:
        raise SystemExit(1)


def _ensure_experiment(client: httpx.Client) -> None:
    resp = client.post(
        "/v1/experiments",
        json={
            "programme": _PROGRAMME,
            "slug": _EXPERIMENT_SLUG,
            "title": "Harness contract E2E validation",
            "hypothesis": "The queue/lease contract (spec §6a) works against a live deployment, not just unit tests.",
            "tags": [_PROGRAMME, "e2e-test"],
            "status": "running",
        },
    )
    if resp.status_code not in (201, 409):
        sys.exit(f"failed to create test experiment: {resp.status_code} {resp.text}")


def run_validation(base_url: str, api_key: str) -> None:
    client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    _ensure_experiment(client)

    _step("Enqueue runs (plan)")
    dep_slug = _run_slug("dependency")
    dependency = client.post(
        f"/v1/experiments/{_EXPERIMENT_SLUG}/runs",
        json={
            "slug": dep_slug,
            "status": "queued",
            "priority": 5,
            "est_seconds": 60,
            "requirements": {"backend": "mock"},
            "workspec": {"code": {"entrypoint": "true"}},
        },
    ).json()
    _check(dependency["status"] == "queued", f"dependency run {dep_slug} enqueued as queued")

    main_slug = _run_slug("main")
    main_run = client.post(
        f"/v1/experiments/{_EXPERIMENT_SLUG}/runs",
        json={
            "slug": main_slug,
            "status": "queued",
            "priority": 10,
            "est_seconds": 120,
            "depends_on": [dependency["id"]],
            "requirements": {"backend": "mock"},
            "workspec": {"code": {"entrypoint": "python train.py"}},
        },
    ).json()
    _check(main_run["status"] == "queued", f"main run {main_slug} enqueued, depends_on dependency")

    _step("Dependency gating")
    ready = client.get("/v1/queue", params={"backend": "mock"}).json()
    ready_slugs = {r["slug"] for r in ready}
    _check(dep_slug in ready_slugs, "dependency run is ready")
    _check(main_slug not in ready_slugs, "main run is NOT ready (depends_on unmet)")

    _step("Claim (atomic, packed)")
    claimed = client.post(
        "/v1/queue/claim", json={"backend": "mock", "session_seconds": 90, "claimed_by": "mock-worker-1"}
    ).json()
    _check(
        len(claimed) == 1 and claimed[0]["slug"] == dep_slug,
        "claim packs only the dependency run (main is gated; its 120s wouldn't fit a 90s budget anyway)",
    )
    dep_run_id = claimed[0]["id"]
    _check(claimed[0]["status"] == "claimed", "claimed run status is 'claimed'")
    _check(claimed[0]["claimed_by"] == "mock-worker-1", "claimed_by recorded")

    _step("Heartbeat")
    renewed = client.post(f"/v1/runs/{dep_run_id}/lease", json={}).json()
    _check(renewed["status"] == "running", "first lease renewal transitions claimed -> running")

    _step("Checkpoint upload")
    presign = client.post(
        f"/v1/runs/{dep_run_id}/artifacts/presign", json={"filename": "checkpoint.bin", "kind": "checkpoint"}
    )
    if presign.status_code == 501:
        print("  R2 not configured on this deployment — skipping upload, using a placeholder URI")
        artifact_uri = f"s3://placeholder/{dep_run_id}/checkpoint.bin"
    else:
        presign_data = presign.json()
        upload = httpx.put(presign_data["upload_url"], content=b"mock checkpoint bytes")
        _check(upload.status_code == 200, "checkpoint uploaded to presigned URL")
        artifact_uri = presign_data["uri"]

    registered = client.post(
        f"/v1/runs/{dep_run_id}/artifacts", json={"kind": "checkpoint", "uri": artifact_uri}
    )
    _check(registered.status_code == 201, "checkpoint artifact registered")
    artifact_id = registered.json()["id"]

    if presign.status_code != 501:
        download = client.get(f"/v1/artifacts/{artifact_id}/download", follow_redirects=True)
        _check(
            download.status_code == 200 and download.content == b"mock checkpoint bytes",
            "checkpoint downloads back byte-identical via presigned GET",
        )

    _step("Submit result, mark completed")
    client.post(f"/v1/runs/{dep_run_id}/results", json={"name": "loss", "value": 0.42, "verdict": "pass"})
    completed = client.patch(f"/v1/runs/{dep_run_id}", json={"status": "completed", "cost_usd": 0.03}).json()
    _check(completed["status"] == "completed", "run marked completed with cost recorded")

    _step("Dependency now satisfied")
    ready = client.get("/v1/queue", params={"backend": "mock"}).json()
    _check(main_slug in {r["slug"] for r in ready}, "main run is now ready (dependency completed)")

    _step("Lease expiry (simulated dead worker)")
    claimed2 = client.post(
        "/v1/queue/claim",
        json={"backend": "mock", "session_seconds": 600, "claimed_by": "mock-worker-2", "lease_seconds": 1},
    ).json()
    _check(
        len(claimed2) == 1 and claimed2[0]["slug"] == main_slug,
        "main run claimed by worker-2 with a 1s lease",
    )
    main_run_id = claimed2[0]["id"]

    time.sleep(2)  # let the 1s lease actually lapse
    sweep1 = client.post("/v1/queue/sweep").json()
    _check(sweep1["requeued"] >= 1, f"sweep requeues the expired-lease run (requeued={sweep1['requeued']})")

    refetched = client.get(f"/v1/runs/{main_run_id}").json()
    _check(
        refetched["status"] == "queued" and refetched["claim_attempts"] == 1,
        "run is back in queued with claim_attempts incremented",
    )

    _step("Repeated lease expiry escalates to 'lost'")
    for _ in range(_MAX_CLAIM_ATTEMPTS - 1):
        client.post(
            "/v1/queue/claim",
            json={
                "backend": "mock",
                "session_seconds": 600,
                "claimed_by": "mock-worker-retry",
                "lease_seconds": 1,
            },
        )
        time.sleep(2)
        client.post("/v1/queue/sweep")

    final = client.get(f"/v1/runs/{main_run_id}").json()
    _check(
        final["status"] == "lost",
        f"after {_MAX_CLAIM_ATTEMPTS} lease expiries the run is marked 'lost' (got {final['status']!r})",
    )

    _step("Cancel a fresh queued run")
    cancel_slug = _run_slug("cancel-me")
    cancel_run = client.post(
        f"/v1/experiments/{_EXPERIMENT_SLUG}/runs", json={"slug": cancel_slug, "status": "queued"}
    ).json()
    cancelled = client.patch(f"/v1/runs/{cancel_run['id']}", json={"status": "cancelled"}).json()
    _check(cancelled["status"] == "cancelled", "a fresh queued run can be cancelled")

    print("\nAll harness-contract checks passed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--api-key",
        required=True,
        help="Bearer key with read|write|admin scope (admin needed for /v1/queue/sweep)",
    )
    args = parser.parse_args()
    run_validation(args.base_url, args.api_key)


if __name__ == "__main__":
    main()
