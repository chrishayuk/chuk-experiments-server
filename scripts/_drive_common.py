"""Shared helpers for the archive_*_to_drive.py scripts — Google Drive auth,
folder/file upload, and a local manifest for resumable reruns. Mirrors
_migrate_common.py's role for the migrate_*.py scripts.

Requires the `archive` extra: `uv sync --extra archive`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

#: drive.file only — the app can see/manage files it creates itself, never
#: the rest of the user's Drive.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

#: Folders are looked up by (name, parent) rather than relying on a
#: uniqueness constraint — Drive doesn't enforce one, unlike a filesystem.
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def get_drive_service(client_id: str, client_secret: str, refresh_token: str):
    """Builds a Drive v3 service directly from an already-minted refresh
    token — reuses gpu-training-harness's existing OAuth client (see that
    project's scripts/authorize-drive.py) rather than registering a new
    one, so there's no interactive consent step: the token's already
    authorized, just needs a fresh access token off the back of it."""
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=DRIVE_SCOPES,
    )
    creds.refresh(GoogleAuthRequest())
    return build("drive", "v3", credentials=creds)


def ensure_folder(service, name: str, parent_id: str | None) -> str:
    """Get-or-create a Drive folder by name under a parent, returning its id."""
    query = f"name = '{name}' and mimeType = '{_FOLDER_MIME_TYPE}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    existing = results.get("files", [])
    if existing:
        return existing[0]["id"]

    metadata: dict[str, Any] = {"name": name, "mimeType": _FOLDER_MIME_TYPE}
    if parent_id:
        metadata["parents"] = [parent_id]
    created = service.files().create(body=metadata, fields="id").execute()
    return created["id"]


def ensure_folder_path(service, root_id: str, parts: tuple[str, ...]) -> str:
    """ensure_folder, applied down a chain of path components — e.g.
    ("chuk-mlx", "cot_vocab_alignment", "checkpoints") under the archive
    root, creating each level as needed."""
    folder_id = root_id
    for part in parts:
        folder_id = ensure_folder(service, part, folder_id)
    return folder_id


def upload_file(service, local_path: Path, parent_id: str) -> str:
    """Resumable upload of a single file, returns the created Drive file id."""
    media = MediaFileUpload(str(local_path), resumable=True)
    created = (
        service.files()
        .create(body={"name": local_path.name, "parents": [parent_id]}, media_body=media, fields="id")
        .execute()
    )
    return created["id"]


def should_skip(path: Path) -> bool:
    """Symlinks (avoid double-archiving content already captured elsewhere
    in the same tree) and .git (source control history, not experiment
    data) are skipped, not silently followed/uploaded."""
    return path.is_symlink() or ".git" in path.parts


def iter_archivable_files(local_dir: Path):
    """Yields every file under local_dir that upload_directory/verify_directory
    would archive — os.walk with followlinks=False so a symlinked
    subdirectory is never traversed into, plus should_skip for file
    symlinks and any stray .git/ dir. Shared so upload and verify can never
    silently disagree on what counts."""
    for dirpath, dirnames, filenames in os.walk(local_dir, followlinks=False):
        current = Path(dirpath)
        if ".git" in current.parts:
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
