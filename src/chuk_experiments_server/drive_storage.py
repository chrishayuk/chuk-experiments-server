"""Google Drive artifact storage — the server-side counterpart to storage.py's
R2 helpers, for artifacts an agent has bytes for right now (config/log/small
dataset files) rather than a checkpoint already sitting in object storage.

Shares its OAuth pattern with scripts/_drive_common.py (drive.file scope,
refresh-token-only, no interactive consent — reuses gpu-training-harness's
already-authorized OAuth client) and provides the reusable pieces
(get_drive_service/ensure_folder/ensure_folder_path) that both this module
and the standalone archive_*_to_drive.py scripts import, so there's exactly
one implementation of "how we talk to Drive," not two that could drift.
"""

import io
from typing import Any

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from .config import settings

#: drive.file only — the app can see/manage files it creates itself, never
#: the rest of the user's Drive.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

#: Folders are looked up by (name, parent) rather than relying on a
#: uniqueness constraint — Drive doesn't enforce one, unlike a filesystem.
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

#: Root folder every artifact this server ever writes to Drive lives under —
#: matches the historical archive_*_to_drive.py scripts' own root, so live
#: agent-registered artifacts and the bulk historical archive share one
#: browsable tree rather than two unrelated ones.
ARCHIVE_ROOT_NAME = "chuk-experiments-archive"

_client = None


def get_drive_service(client_id: str, client_secret: str, refresh_token: str):
    """Builds a Drive v3 service directly from an already-minted refresh
    token — no interactive consent step: the token's already authorized,
    just needs a fresh access token off the back of it."""
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


def get_client():
    """Lazily builds (and caches) a Drive service from this server's own
    configured credentials — mirrors storage.get_client()'s role for R2."""
    global _client
    if _client is None:
        _client = get_drive_service(
            settings.google_drive_client_id,
            settings.google_drive_client_secret,
            settings.google_drive_refresh_token,
        )
    return _client


def _escape_drive_query_value(value: str) -> str:
    """Drive's query language uses '...' string literals with the same
    backslash-escaping convention SQL does — a bare `'` in `name` (e.g. an
    artifact name a caller controls) would otherwise break out of the
    literal and reshape the query. Escape backslashes first so an
    escaped-then-escaped quote doesn't collide."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def ensure_folder(service, name: str, parent_id: str | None) -> str:
    """Get-or-create a Drive folder by name under a parent, returning its id."""
    query = (
        f"name = '{_escape_drive_query_value(name)}' and mimeType = '{_FOLDER_MIME_TYPE}' and trashed = false"
    )
    if parent_id:
        query += f" and '{_escape_drive_query_value(parent_id)}' in parents"
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
    """ensure_folder, applied down a chain of path components."""
    folder_id = root_id
    for part in parts:
        folder_id = ensure_folder(service, part, folder_id)
    return folder_id


def upload_bytes(service, filename: str, content: bytes, parent_id: str) -> str:
    """Uploads in-memory content (bytes that arrived over HTTP, not already
    on the server's disk) — the server-side counterpart to
    scripts/_drive_common.py's upload_file, which uploads from a local path
    a standalone migration script already has on disk. Returns the created
    Drive file id."""
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/octet-stream", resumable=False)
    created = (
        service.files()
        .create(body={"name": filename, "parents": [parent_id]}, media_body=media, fields="id")
        .execute()
    )
    return created["id"]


def drive_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"
