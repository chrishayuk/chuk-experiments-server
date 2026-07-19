"""Shared helpers for the archive_*_to_drive.py scripts — a local manifest
for resumable reruns, plus directory-walk upload/verify built on top of
chuk_experiments_server.drive_storage's Drive auth/folder helpers (shared
with the live server's own upload endpoint, so there's exactly one
implementation of "how we talk to Drive", not two that could drift).
Mirrors _migrate_common.py's role for the migrate_*.py scripts.

google-auth/google-api-python-client are regular project dependencies now (drive_storage.py is a core server module), so a normal dev install already has them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from googleapiclient.http import MediaFileUpload

from chuk_experiments_server.drive_storage import (  # noqa: F401 - re-exported for script callers
    DRIVE_SCOPES,
    ensure_folder,
    ensure_folder_path,
    get_drive_service,
)


def upload_file(service, local_path: Path, parent_id: str) -> str:
    """Resumable upload of a single file, returns the created Drive file id."""
    media = MediaFileUpload(str(local_path), resumable=True)
    created = (
        service.files()
        .create(body={"name": local_path.name, "parents": [parent_id]}, media_body=media, fields="id")
        .execute()
    )
    return created["id"]


_SKIP_DIR_NAMES = (".git", ".claude")


def should_skip(path: Path) -> bool:
    """Symlinks (avoid double-archiving content already captured elsewhere
    in the same tree), .git (source control history), .claude (local tool
    state), and macOS's .DS_Store are skipped, not silently followed/
    uploaded — none of it is experiment data."""
    if path.is_symlink() or path.name == ".DS_Store":
        return True
    return any(name in path.parts for name in _SKIP_DIR_NAMES)


def iter_archivable_files(local_dir: Path):
    """Yields every file under local_dir that upload_directory/verify_directory
    would archive — os.walk with followlinks=False so a symlinked
    subdirectory is never traversed into, plus should_skip for file
    symlinks and any stray .git/.claude dir. Shared so upload and verify
    can never silently disagree on what counts."""
    for dirpath, dirnames, filenames in os.walk(local_dir, followlinks=False):
        current = Path(dirpath)
        if any(name in current.parts for name in _SKIP_DIR_NAMES):
            dirnames[:] = []
            continue
        for filename in sorted(filenames):
            item = current / filename
            if not should_skip(item):
                yield item


def upload_directory(
    service, local_dir: Path, drive_parent_id: str, manifest: Manifest, relative_root: Path
) -> tuple[int, int]:
    """Recursively uploads local_dir's contents under drive_parent_id,
    mirroring the directory structure, skipping anything already recorded
    in the manifest (resumable) and anything should_skip flags. Returns
    (files_uploaded, bytes_uploaded) — a no-op rerun returns (0, 0)."""
    files_uploaded = 0
    bytes_uploaded = 0
    folder_cache: dict[Path, str] = {local_dir: drive_parent_id}

    def folder_for(path: Path) -> str:
        if path in folder_cache:
            return folder_cache[path]
        parent_id = folder_for(path.parent)
        folder_id = ensure_folder(service, path.name, parent_id)
        folder_cache[path] = folder_id
        return folder_id

    for item in iter_archivable_files(local_dir):
        relative = item.relative_to(relative_root)
        if manifest.has(str(relative)):
            continue
        parent_folder_id = folder_for(item.parent)
        drive_id = upload_file(service, item, parent_folder_id)
        size = item.stat().st_size
        manifest.record(str(relative), drive_id, size)
        files_uploaded += 1
        bytes_uploaded += size

    return files_uploaded, bytes_uploaded


def verify_directory(local_dir: Path, manifest: Manifest, relative_root: Path) -> dict[str, Any]:
    """Compares what's on disk now against what the manifest recorded as
    uploaded — same file-selection rules as upload_directory, so this can
    never disagree with what was actually eligible to upload. Returns a
    dict with local/manifest file counts+bytes and the list of any
    on-disk files missing from the manifest."""
    local_files = list(iter_archivable_files(local_dir))
    local_relatives = {str(item.relative_to(relative_root)) for item in local_files}
    local_bytes = sum(item.stat().st_size for item in local_files)

    manifest_relatives = {
        rel for rel in manifest.entries() if Path(rel).is_relative_to(local_dir.relative_to(relative_root))
    }
    manifest_bytes = sum(manifest.entries()[rel]["size"] for rel in manifest_relatives)

    missing = sorted(local_relatives - manifest_relatives)
    return {
        "local_file_count": len(local_relatives),
        "local_bytes": local_bytes,
        "manifest_file_count": len(manifest_relatives),
        "manifest_bytes": manifest_bytes,
        "missing_from_manifest": missing,
    }


class Manifest:
    """JSON-backed {local_relative_path: {drive_id, size, uploaded_at}} —
    the resumability mechanism standing in for a uniqueness constraint
    Drive itself doesn't enforce. Written after every file so a killed and
    re-run script never re-uploads what's already done."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, Any]] = json.loads(path.read_text()) if path.exists() else {}

    def has(self, relative_path: str) -> bool:
        return relative_path in self.data

    def record(self, relative_path: str, drive_id: str, size: int) -> None:
        self.data[relative_path] = {
            "drive_id": drive_id,
            "size": size,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def entries(self) -> dict[str, dict[str, Any]]:
        return self.data
