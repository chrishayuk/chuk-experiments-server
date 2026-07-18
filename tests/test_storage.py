"""storage.py's presign_put/presign_get call out to R2 over the network (via
boto3), so they're not covered here — build_uri/key_from_uri are pure and
worth locking down since register_artifact/download round-trip through them."""

import pytest

from chuk_experiments_server import storage
from chuk_experiments_server.config import settings


@pytest.fixture(autouse=True)
def _r2_bucket(monkeypatch):
    monkeypatch.setattr(type(settings), "r2_bucket", property(lambda self: "chuk-train"))


def test_build_uri_uses_configured_bucket():
    assert storage.build_uri("runs/1/checkpoint/model.bin") == "s3://chuk-train/runs/1/checkpoint/model.bin"


def test_key_from_uri_round_trips_with_build_uri():
    uri = storage.build_uri("runs/1/checkpoint/model.bin")
    assert storage.key_from_uri(uri) == "runs/1/checkpoint/model.bin"


def test_key_from_uri_rejects_uri_from_a_different_bucket():
    with pytest.raises(ValueError, match="not in bucket"):
        storage.key_from_uri("s3://someone-elses-bucket/runs/1/checkpoint/model.bin")
