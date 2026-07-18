"""build_uri/key_from_uri are pure; get_client/presign_put/presign_get are
also fully local — generate_presigned_url is an HMAC signature, not a
network call — so fake-but-syntactically-valid R2 settings are enough to
test them without hitting Cloudflare."""

import pytest

from chuk_experiments_server import storage
from chuk_experiments_server.config import settings


@pytest.fixture(autouse=True)
def _fake_r2_settings(monkeypatch):
    monkeypatch.setattr(type(settings), "r2_bucket", property(lambda self: "chuk-train"))
    monkeypatch.setattr(
        type(settings), "r2_endpoint_url", property(lambda self: "https://example.r2.cloudflarestorage.com")
    )
    monkeypatch.setattr(type(settings), "r2_access_key_id", property(lambda self: "fake-access-key-id"))
    monkeypatch.setattr(
        type(settings), "r2_secret_access_key", property(lambda self: "fake-secret-access-key")
    )
    storage._client = None
    yield
    storage._client = None


def test_build_uri_uses_configured_bucket():
    assert storage.build_uri("runs/1/checkpoint/model.bin") == "s3://chuk-train/runs/1/checkpoint/model.bin"


def test_key_from_uri_round_trips_with_build_uri():
    uri = storage.build_uri("runs/1/checkpoint/model.bin")
    assert storage.key_from_uri(uri) == "runs/1/checkpoint/model.bin"


def test_key_from_uri_rejects_uri_from_a_different_bucket():
    with pytest.raises(ValueError, match="not in bucket"):
        storage.key_from_uri("s3://someone-elses-bucket/runs/1/checkpoint/model.bin")


def test_get_client_lazily_creates_and_reuses():
    first = storage.get_client()
    second = storage.get_client()
    assert first is second


def test_presign_put_returns_url_for_bucket_and_key():
    url = storage.presign_put("runs/1/checkpoint/model.bin")
    assert "chuk-train" in url
    assert "runs/1/checkpoint/model.bin" in url


def test_presign_put_includes_content_type_when_given():
    url = storage.presign_put("runs/1/checkpoint/model.bin", content_type="application/octet-stream")
    assert "Content-Type" in url or "content-type" in url.lower()


def test_presign_get_returns_url_for_bucket_and_key():
    url = storage.presign_get("runs/1/checkpoint/model.bin")
    assert "chuk-train" in url
    assert "runs/1/checkpoint/model.bin" in url
