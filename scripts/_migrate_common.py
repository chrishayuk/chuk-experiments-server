"""Shared helpers for the migrate_*.py scripts — kept in one place so all
four sources get the same slugify rules, the same programme display-name
overrides, and the same idempotent-rerun behavior (409 on an
already-migrated slug is a quiet skip, not an error)."""

import re
import sys

import httpx

#: get_or_create_programme humanizes an unseen slug ("state-construction" ->
#: "State Construction"), which is wrong for acronyms — override those here
#: rather than passing a per-script one-off name.
PROGRAMME_NAME_OVERRIDES = {
    "larql": "LARQL",
    "chuk-mlx": "CHUK-MLX",
}


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return text.strip("-")


def programme_name(slug: str) -> str | None:
    return PROGRAMME_NAME_OVERRIDES.get(slug)


def create_experiment(client: httpx.Client, payload: dict) -> dict | None:
    """POST /v1/experiments. Returns the created experiment dict, or None if
    it already exists (409 — printed and skipped, safe to rerun the script)
    or the request otherwise failed (printed to stderr)."""
    payload.setdefault("programme_name", programme_name(payload["programme"]))
    resp = client.post("/v1/experiments", json=payload)
    if resp.status_code == 409:
        print(f"= '{payload['slug']}' already exists, skipping")
        return None
    if resp.status_code >= 400:
        print(f"! failed to create experiment '{payload['slug']}': {resp.status_code} {resp.text}", file=sys.stderr)
        return None
    return resp.json()


def create_run(client: httpx.Client, experiment_slug: str, payload: dict) -> dict | None:
    """POST /v1/experiments/{slug}/runs — same 409-is-a-skip behavior as create_experiment."""
    resp = client.post(f"/v1/experiments/{experiment_slug}/runs", json=payload)
    if resp.status_code == 409:
        print(f"= run '{payload.get('slug')}' on '{experiment_slug}' already exists, skipping")
        return None
    if resp.status_code >= 400:
        print(f"! failed to create run on '{experiment_slug}': {resp.status_code} {resp.text}", file=sys.stderr)
        return None
    return resp.json()
