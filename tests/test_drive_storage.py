"""ensure_folder/ensure_folder_path/upload_bytes are exercised against a fake
Drive-API-shaped service object (mirroring googleapiclient's chained-call
interface: service.files().list(...).execute()) rather than a real Drive
connection. get_drive_service/get_client mock Credentials/build directly —
Credentials.refresh() is a real network call to Google's token endpoint,
unlike storage.get_client's boto3 client construction, which is purely
local and needs no such mocking."""

import pytest

from chuk_experiments_server import drive_storage
from chuk_experiments_server.config import settings


class _Execute:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeFilesResource:
    def __init__(self, list_response=None):
        self._list_response = list_response or {"files": []}
        self.create_calls: list[dict] = []

    def list(self, q, fields):
        return _Execute(self._list_response)

    def create(self, body, fields=None, media_body=None):
        self.create_calls.append({"body": body, "media_body": media_body})
        return _Execute({"id": f"created-{len(self.create_calls)}"})


class _FakeDriveService:
    def __init__(self, files_resource):
        self._files_resource = files_resource

    def files(self):
        return self._files_resource


def test_ensure_folder_returns_existing_id_without_creating():
    files = _FakeFilesResource(list_response={"files": [{"id": "existing-id", "name": "x"}]})
    folder_id = drive_storage.ensure_folder(_FakeDriveService(files), "x", "parent-id")
    assert folder_id == "existing-id"
    assert files.create_calls == []


def test_ensure_folder_creates_when_missing():
    files = _FakeFilesResource()
    folder_id = drive_storage.ensure_folder(_FakeDriveService(files), "new-folder", "parent-id")
    assert folder_id == "created-1"
    assert files.create_calls[0]["body"]["name"] == "new-folder"
    assert files.create_calls[0]["body"]["parents"] == ["parent-id"]


def test_ensure_folder_creates_without_parent_when_none():
    files = _FakeFilesResource()
    drive_storage.ensure_folder(_FakeDriveService(files), "root-folder", None)
    assert "parents" not in files.create_calls[0]["body"]


def test_ensure_folder_path_chains_through_each_part():
    files = _FakeFilesResource()
    folder_id = drive_storage.ensure_folder_path(_FakeDriveService(files), "root-id", ("a", "b", "c"))
    assert folder_id == "created-3"
    assert len(files.create_calls) == 3


def test_upload_bytes_returns_created_file_id():
    files = _FakeFilesResource()
    file_id = drive_storage.upload_bytes(_FakeDriveService(files), "hello.txt", b"hello world", "parent-id")
    assert file_id == "created-1"
    assert files.create_calls[0]["body"]["name"] == "hello.txt"
    assert files.create_calls[0]["body"]["parents"] == ["parent-id"]


def test_drive_url_format():
    assert drive_storage.drive_url("abc123") == "https://drive.google.com/file/d/abc123/view"


class _FakeCredentials:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.refreshed = False

    def refresh(self, request):
        self.refreshed = True


def test_get_drive_service_refreshes_credentials_and_builds(monkeypatch):
    created = {}

    def fake_credentials(**kwargs):
        creds = _FakeCredentials(**kwargs)
        created["creds"] = creds
        return creds

    built = {}

    def fake_build(name, version, credentials=None):
        built.update(name=name, version=version, credentials=credentials)
        return "fake-drive-service"

    monkeypatch.setattr(drive_storage, "Credentials", fake_credentials)
    monkeypatch.setattr(drive_storage, "build", fake_build)

    service = drive_storage.get_drive_service("client-id", "client-secret", "refresh-token")

    assert service == "fake-drive-service"
    assert created["creds"].refreshed
    assert created["creds"].kwargs["refresh_token"] == "refresh-token"
    assert built["name"] == "drive"
    assert built["credentials"] is created["creds"]


@pytest.fixture(autouse=True)
def _reset_client_cache():
    drive_storage._client = None
    yield
    drive_storage._client = None


def test_get_client_lazily_creates_and_caches(monkeypatch):
    monkeypatch.setattr(type(settings), "google_drive_client_id", property(lambda self: "id"))
    monkeypatch.setattr(type(settings), "google_drive_client_secret", property(lambda self: "secret"))
    monkeypatch.setattr(type(settings), "google_drive_refresh_token", property(lambda self: "token"))

    calls = []
    monkeypatch.setattr(
        drive_storage, "get_drive_service", lambda cid, secret, token: calls.append(1) or "fake-service"
    )

    first = drive_storage.get_client()
    second = drive_storage.get_client()
    assert first is second
    assert first == "fake-service"
    assert len(calls) == 1
