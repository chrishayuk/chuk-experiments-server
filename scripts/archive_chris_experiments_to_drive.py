#!/usr/bin/env python3
"""Archive chris-experiments/ raw data (results/checkpoints/logs per
experiment directory) to Google Drive, linking each archived directory back
to every matching chuk-experiments-server experiment record via
register_artifact.

Unlike chuk-mlx, slugs don't derive from directory names — the only
reliable directory<->slug signal is INDEX.md's own **Path:** bullet
(preserved verbatim in the DB as design.path/config.path by
migrate_chris_experiments.py). This script re-parses the same INDEX.md with
the exact same parse_index/experiment_id_and_title/build_slug logic to
reconstruct that mapping deterministically — including real many-to-one
cases (e.g. state-construction/73_all_layer_fisher/ backs 3 separate
slugs, since build_slug's collision-suffixing depends on iteration order
matching the original migration exactly).

155 of chris-experiments/'s directories have a Path: bullet (across 8
programme dirs: foundations, compilation, routing, shannon, mechinterp,
state-construction, grammar, larql). Everything else — top-level
directories never referenced in INDEX.md at all (fleet, paper, ...), root-
level loose files (README.md, INDEX.md itself, ...), and unindexed content
sitting right next to a programme dir's real experiment subdirectories
(grammar/data/, a bulk-data cache) — is still archived, by a single
catch-all pass after the per-path uploads (see compute_residual_files),
just without the register_artifact step, since there's no experiment to
attach it to. An earlier version of this script only caught whole
unreferenced top-level directories, silently missing the "partially
indexed programme dir" case — 194 real files on a from-scratch chris-
experiments run, root-caused via compute_residual_files against the real
checkout before this script archived anything.

google-auth/google-api-python-client are regular project dependencies now (drive_storage.py is a core server module), so a normal dev install already has them.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from _drive_common import (
    Manifest,
    ensure_folder,
    get_drive_service,
    iter_archivable_files,
    upload_directory,
    verify_directory,
)
from chuk_experiments_server.constants import GDRIVE_URI_PREFIX
from migrate_chris_experiments import build_slug, experiment_id_and_title, parse_index

load_dotenv()

_ARCHIVE_ROOT_NAME = "chuk-experiments-archive"
_SOURCE_NAME = "chris-experiments"
_HISTORICAL_RUN_SLUG = "historical"


def build_path_to_slugs(index_text: str) -> dict[str, list[str]]:
    """Reconstructs the same path -> slug(s) mapping migrate_chris_experiments.py
    produced, by re-running its own parse_index/build_slug over the same
    INDEX.md content in the same order — build_slug's collision-suffixing
    depends on iteration order, so this must match exactly to reproduce
    the same slugs already sitting in the DB."""
    experiments = parse_index(index_text)
    seen_slugs: set[str] = set()
    path_to_slugs: dict[str, list[str]] = {}
    for exp in experiments:
        id_part, title = experiment_id_and_title(exp.heading)
        slug = build_slug(exp.programme_slug, id_part, title, seen_slugs)
        if exp.path:
            path_to_slugs.setdefault(exp.path.rstrip("/"), []).append(slug)
    return path_to_slugs


def discover_top_level_dirs(root: Path) -> list[Path]:
    dirs = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            print(f"  (skipping top-level symlink: {child.name})")
            continue
        if not child.is_dir() or child.name.startswith((".", "__")):
            continue
        dirs.append(child)
    return dirs


def compute_residual_files(root: Path, path_to_slugs: dict[str, list[str]]) -> list[Path]:
    """Every archivable file under root NOT reachable through an INDEX.md
    Path: bullet — what the final catch-all pass in run_archive uploads.
    This is deliberately NOT "whole top-level dirs with zero Path:
    references" (the bug this replaces): a programme dir like grammar/
    has some Path-referenced experiment subdirectories AND real unindexed
    content sitting right next to them (grammar/data/, grammar/README.md)
    — checking only whole top-level dirs missed all of that, silently,
    every run. Computed without touching Drive, so --dry-run stays fully
    offline."""
    covered = {
        item
        for path in path_to_slugs
        if (root / path).is_dir()
        for item in iter_archivable_files(root / path)
    }
    return [item for item in iter_archivable_files(root) if item not in covered]


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
    index_path: Path,
    base_url: str,
    api_key: str | None,
    drive_client_id: str | None,
    drive_client_secret: str | None,
    drive_refresh_token: str | None,
    manifest_path: Path,
    dry_run: bool,
) -> None:
    path_to_slugs = build_path_to_slugs(index_path.read_text())
    all_top_level_dirs = discover_top_level_dirs(root)
    unlinked_top_level = [
        d for d in all_top_level_dirs if not any(p.startswith(d.name + "/") for p in path_to_slugs)
    ]

    residual_files = compute_residual_files(root, path_to_slugs)

    print(f"Parsed {len(path_to_slugs)} Path-bearing directories from {index_path}")
    print(
        f"{len(unlinked_top_level)} of {len(all_top_level_dirs)} top-level dirs have no Path: reference"
        f" at all: {[d.name for d in unlinked_top_level]}"
    )
    print(
        f"{len(residual_files)} files fall outside every Path: bullet (root-level loose files, plus"
        " unindexed content sitting next to a partially-indexed programme dir's real experiment"
        " subdirectories) — archived by the final catch-all pass, never linked to any experiment"
    )

    if dry_run:
        for path, slugs in sorted(path_to_slugs.items()):
            print(f"  {path:60s} -> {slugs}")
        print(f"  (+{len(residual_files)} residual files -> archived, unlinked)")
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
    source_root_id = ensure_folder(service, _SOURCE_NAME, archive_root_id)

    folder_id_by_relative_path: dict[str, str] = {}

    def folder_for_relative_path(relative_path: str) -> str:
        """ensure_folder for each path component under source_root_id,
        e.g. "foundations/01_gate_synthesis" -> nested folders, caching
        each level so sibling experiments under the same programme dir
        don't re-create/re-lookup its folder every time."""
        if relative_path in folder_id_by_relative_path:
            return folder_id_by_relative_path[relative_path]
        folder_id = source_root_id
        accumulated = ""
        for part in Path(relative_path).parts:
            accumulated = f"{accumulated}/{part}" if accumulated else part
            if accumulated in folder_id_by_relative_path:
                folder_id = folder_id_by_relative_path[accumulated]
            else:
                folder_id = ensure_folder(service, part, folder_id)
                folder_id_by_relative_path[accumulated] = folder_id
        return folder_id

    total_files = total_bytes = linked = unlinked = 0

    for path, slugs in sorted(path_to_slugs.items()):
        local_dir = root / path
        if not local_dir.is_dir():
            print(f"  ! Path '{path}' from INDEX.md doesn't exist on disk — skipping", file=sys.stderr)
            continue

        folder_id = folder_for_relative_path(path)
        files, size = upload_directory(service, local_dir, folder_id, manifest, root)
        total_files += files
        total_bytes += size
        print(f"  {path:60s} +{files} files, +{size:,} bytes -> {slugs}")

        for slug in slugs:
            run_id = find_historical_run_id(client, slug)
            if run_id is None:
                print(
                    f"    ! expected DB record '{slug}' not found or has no historical run", file=sys.stderr
                )
                unlinked += 1
                continue
            register_archive_artifact(client, run_id, folder_id, path)
            linked += 1

    # Catch-all: root-level loose files (README.md, INDEX.md itself, ...)
    # and any content nested inside a programme dir that has SOME
    # Path-referenced experiment subdirectories but isn't itself indexed
    # (grammar/data/, a bulk-data cache sitting right next to grammar's
    # real experiment dirs) — everything the per-path loop above didn't
    # already reach. One recursive pass over `root`, resumable via the
    # same manifest so nothing already uploaded gets re-sent; skips
    # anything should_skip flags (.git, .claude, .DS_Store, symlinks).
    catch_all_files, catch_all_bytes = upload_directory(service, root, source_root_id, manifest, root)
    total_files += catch_all_files
    total_bytes += catch_all_bytes
    print(
        f"  (catch-all, unindexed)                                      +{catch_all_files} files, +{catch_all_bytes:,} bytes"
    )

    print(
        f"Done: {total_files} files, {total_bytes:,} bytes uploaded."
        f" {linked} artifacts linked, {unlinked} link failures."
    )


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
    parser.add_argument("--source", type=Path, default=Path("../chris-experiments"))
    parser.add_argument("--index", type=Path, default=None, help="Defaults to <source>/INDEX.md")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", help="Bearer key with write scope (required unless --dry-run)")
    parser.add_argument("--drive-client-id", default=os.environ.get("GOOGLE_DRIVE_CLIENT_ID"))
    parser.add_argument("--drive-client-secret", default=os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET"))
    parser.add_argument("--drive-refresh-token", default=os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN"))
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).parent / ".drive_manifest_chris_experiments.json"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--verify", action="store_true", help="Compare local files against the manifest instead of uploading"
    )
    args = parser.parse_args()
    index_path = args.index or (args.source / "INDEX.md")

    if args.verify:
        run_verify(args.source, args.manifest)
    else:
        run_archive(
            args.source,
            index_path,
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
