#!/usr/bin/env python3
"""Archive chuk-mlx/experiments/ raw data (checkpoints, logs, run outputs)
to Google Drive, linking each archived directory back to its matching
chuk-experiments-server experiment record via register_artifact.

Slug is always chuk-mlx-{slugify(dirname)} — a pure dirname formula (see
migrate_chuk_mlx.py's run_migration), no API lookup needed. 2 of the 33
subdirectories (probe_classifier_semantic, probe_classifier_tinyllama) have
no matching DB record (no EXPERIMENT.md/RESULTS.md/README.md, excluded by
migrate_chuk_mlx.py's discover_experiments filter) — archived anyway, just
without artifact registration.

google-auth/google-api-python-client are regular project dependencies now (drive_storage.py is a core server module), so a normal dev install already has them.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from _drive_common import Manifest, ensure_folder, get_drive_service, upload_directory, verify_directory
from _migrate_common import slugify
from chuk_experiments_server.constants import GDRIVE_URI_PREFIX
from migrate_chuk_mlx import discover_experiments

load_dotenv()

_PROGRAMME_SLUG = "chuk-mlx"
_ARCHIVE_ROOT_NAME = "chuk-experiments-archive"
_HISTORICAL_RUN_SLUG = "historical"


def find_historical_run_id(client: httpx.Client, slug: str) -> str | None:
    resp = client.get(f"/v1/experiments/{slug}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    for run in resp.json().get("runs", []):
        if run["slug"] == _HISTORICAL_RUN_SLUG:
            return run["id"]
    return None


def register_archive_artifact(client: httpx.Client, run_id: str, folder_id: str, source_path: str) -> None:
    resp = client.post(
        f"/v1/runs/{run_id}/artifacts",
        json={
            "kind": "other",
            "uri": f"{GDRIVE_URI_PREFIX}{folder_id}",
            "meta": {
                "drive_url": f"https://drive.google.com/drive/folders/{folder_id}",
                "source_path": source_path,
            },
        },
    )
    if resp.status_code >= 400:
        print(
            f"! failed to register artifact for run {run_id}: {resp.status_code} {resp.text}",
            file=sys.stderr,
        )


def run_archive(
    root: Path,
    base_url: str,
    api_key: str | None,
    drive_client_id: str | None,
    drive_client_secret: str | None,
    drive_refresh_token: str | None,
    manifest_path: Path,
    dry_run: bool,
) -> None:
    all_dirs = sorted(c for c in root.iterdir() if c.is_dir() and not c.name.startswith((".", "__")))
    eligible = {d.name for d in discover_experiments(root)}
    print(f"Found {len(all_dirs)} directories under {root} ({len(eligible)} DB-linkable)")

    if dry_run:
        for d in all_dirs:
            status = "linkable" if d.name in eligible else "unlinked (no writeup file)"
            print(f"  {d.name:35s} {status}")
        return

    if not api_key:
        sys.exit("--api-key is required unless --dry-run")
    if not (drive_client_id and drive_client_secret and drive_refresh_token):
        sys.exit(
            "--drive-client-id/--drive-client-secret/--drive-refresh-token (or the "
            "GOOGLE_DRIVE_CLIENT_ID/GOOGLE_DRIVE_CLIENT_SECRET/GOOGLE_DRIVE_REFRESH_TOKEN env vars) "
            "are required unless --dry-run"
        )

    service = get_drive_service(drive_client_id, drive_client_secret, drive_refresh_token)
    manifest = Manifest(manifest_path)
    client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)

    archive_root_id = ensure_folder(service, _ARCHIVE_ROOT_NAME, None)
    source_root_id = ensure_folder(service, _PROGRAMME_SLUG, archive_root_id)

    total_files = total_bytes = linked = unlinked = 0

    for exp_dir in all_dirs:
        folder_id = ensure_folder(service, exp_dir.name, source_root_id)
        files, size = upload_directory(service, exp_dir, folder_id, manifest, root)
        total_files += files
        total_bytes += size
        print(f"  {exp_dir.name:35s} +{files} files, +{size:,} bytes")

        if exp_dir.name not in eligible:
            print(f"    (archived, unlinked — no DB record for {exp_dir.name})")
            unlinked += 1
            continue

        slug = f"{_PROGRAMME_SLUG}-{slugify(exp_dir.name)}"
        run_id = find_historical_run_id(client, slug)
        if run_id is None:
            print(
                f"    ! expected DB record '{slug}' not found or has no historical run"
                " — treating as unlinked",
                file=sys.stderr,
            )
            unlinked += 1
            continue

        register_archive_artifact(client, run_id, folder_id, f"experiments/{exp_dir.name}")
        linked += 1

    print(f"Done: {total_files} files, {total_bytes:,} bytes uploaded. {linked} linked, {unlinked} unlinked.")


def run_verify(root: Path, manifest_path: Path) -> None:
    manifest = Manifest(manifest_path)
    result = verify_directory(root, manifest, root)
    print(f"Local:    {result['local_file_count']} files, {result['local_bytes']:,} bytes")
    print(f"Manifest: {result['manifest_file_count']} files, {result['manifest_bytes']:,} bytes")
    if result["missing_from_manifest"]:
        print(f"MISSING from manifest ({len(result['missing_from_manifest'])}):")
        for path in result["missing_from_manifest"]:
            print(f"  {path}")
        sys.exit(1)
    print("OK — every local file is accounted for in the manifest.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", type=Path, default=Path("../chuk-mlx/experiments"))
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", help="Bearer key with write scope (required unless --dry-run)")
    parser.add_argument("--drive-client-id", default=os.environ.get("GOOGLE_DRIVE_CLIENT_ID"))
    parser.add_argument("--drive-client-secret", default=os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET"))
    parser.add_argument("--drive-refresh-token", default=os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN"))
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).parent / ".drive_manifest_chuk_mlx.json"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--verify", action="store_true", help="Compare local files against the manifest instead of uploading"
    )
    args = parser.parse_args()

    if args.verify:
        run_verify(args.source, args.manifest)
    else:
        run_archive(
            args.source,
            args.base_url,
            args.api_key,
            args.drive_client_id,
            args.drive_client_secret,
            args.drive_refresh_token,
            args.manifest,
            args.dry_run,
        )


if __name__ == "__main__":
    main()
