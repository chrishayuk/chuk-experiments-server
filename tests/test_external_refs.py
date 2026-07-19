"""external_refs.py tests — URI build/parse are pure string logic, tested
directly; verify_git_ref/verify_hf_ref hit httpx.AsyncClient, faked the same
way test_webauth.py fakes its own OAuth httpx calls (a duck-typed async
context manager returning a canned response), so no real network traffic
happens in the suite."""

import pytest

from chuk_experiments_server import external_refs
from chuk_experiments_server.config import settings


class _FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeAsyncClient:
    calls = []

    def __init__(self, response, **kwargs):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        type(self).calls.append({"url": url, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _install_fake_client(monkeypatch, response):
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(external_refs.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(response, **kw))
    return _FakeAsyncClient


# --- URI build/parse -------------------------------------------------------


def test_build_and_parse_git_uri_round_trip():
    uri = external_refs.build_git_uri("chrishayuk", "chuk-mlx", "abc123")
    assert uri == "git+https://github.com/chrishayuk/chuk-mlx@abc123"
    assert external_refs.parse_git_uri(uri) == ("github.com", "chrishayuk", "chuk-mlx", "abc123")


def test_parse_git_uri_strips_dot_git_suffix():
    uri = "git+https://github.com/chrishayuk/chuk-mlx.git@abc123"
    assert external_refs.parse_git_uri(uri) == ("github.com", "chrishayuk", "chuk-mlx", "abc123")


def test_parse_git_uri_rejects_non_git_uri():
    with pytest.raises(ValueError):
        external_refs.parse_git_uri("https://example.com/whatever")


def test_parse_git_uri_rejects_missing_commit():
    with pytest.raises(ValueError):
        external_refs.parse_git_uri("git+https://github.com/chrishayuk/chuk-mlx")


def test_build_and_parse_hf_uri_round_trip():
    uri = external_refs.build_hf_uri("model", "chrishayuk/granite-4.1-3b-q4k-vindex", "main")
    assert uri == "hf://model/chrishayuk/granite-4.1-3b-q4k-vindex@main"
    assert external_refs.parse_hf_uri(uri) == (
        "model",
        "chrishayuk/granite-4.1-3b-q4k-vindex",
        "main",
    )


def test_parse_hf_uri_defaults_revision_to_main_when_omitted():
    uri = "hf://dataset/chrishayuk/some-dataset"
    assert external_refs.parse_hf_uri(uri) == ("dataset", "chrishayuk/some-dataset", "main")


def test_build_hf_uri_rejects_invalid_repo_type():
    with pytest.raises(ValueError):
        external_refs.build_hf_uri("checkpoint", "chrishayuk/x", "main")


def test_parse_hf_uri_rejects_non_hf_uri():
    with pytest.raises(ValueError):
        external_refs.parse_hf_uri("s3://bucket/x")


def test_parse_git_uri_rejects_missing_repo_path_segment():
    with pytest.raises(ValueError):
        external_refs.parse_git_uri("git+https://github.com/onlyowner@abc123")


def test_parse_hf_uri_rejects_invalid_repo_type():
    with pytest.raises(ValueError):
        external_refs.parse_hf_uri("hf://checkpoint/chrishayuk/x@main")


def test_parse_hf_uri_rejects_missing_repo_id():
    with pytest.raises(ValueError):
        external_refs.parse_hf_uri("hf://model/")


# --- verify_git_ref ---------------------------------------------------------


async def test_verify_git_ref_verified(monkeypatch):
    _install_fake_client(monkeypatch, _FakeResponse(200))
    result = await external_refs.verify_git_ref("github.com", "chrishayuk", "chuk-mlx", "abc123")
    assert result.status == "verified"


async def test_verify_git_ref_missing(monkeypatch):
    _install_fake_client(monkeypatch, _FakeResponse(404))
    result = await external_refs.verify_git_ref("github.com", "chrishayuk", "chuk-mlx", "deadbeef")
    assert result.status == "missing"


async def test_verify_git_ref_unverifiable_for_non_github_host_makes_no_request(monkeypatch):
    fake_client_cls = _install_fake_client(monkeypatch, _FakeResponse(200))
    result = await external_refs.verify_git_ref("gitlab.com", "someone", "somerepo", "abc123")
    assert result.status == "unverifiable"
    assert fake_client_cls.calls == []


async def test_verify_git_ref_sends_token_as_bearer_header_when_configured(monkeypatch):
    monkeypatch.setattr(type(settings), "github_token", property(lambda self: "gh-secret"))
    fake_client_cls = _install_fake_client(monkeypatch, _FakeResponse(200))
    await external_refs.verify_git_ref("github.com", "chrishayuk", "chuk-mlx", "abc123")
    assert fake_client_cls.calls[0]["headers"]["Authorization"] == "Bearer gh-secret"


async def test_verify_git_ref_unverifiable_on_unexpected_status(monkeypatch):
    _install_fake_client(monkeypatch, _FakeResponse(403))
    result = await external_refs.verify_git_ref("github.com", "chrishayuk", "chuk-mlx", "abc123")
    assert result.status == "unverifiable"


async def test_verify_git_ref_unverifiable_on_network_error(monkeypatch):
    import httpx

    _install_fake_client(monkeypatch, httpx.ConnectError("boom"))
    result = await external_refs.verify_git_ref("github.com", "chrishayuk", "chuk-mlx", "abc123")
    assert result.status == "unverifiable"


# --- verify_hf_ref -----------------------------------------------------------


async def test_verify_hf_ref_verified_existence_only(monkeypatch):
    _install_fake_client(
        monkeypatch, _FakeResponse(200, [{"type": "file", "path": "config.json", "size": 100}])
    )
    result = await external_refs.verify_hf_ref("model", "chrishayuk/granite-4.1-3b-q4k-vindex", "main", None)
    assert result.status == "verified"


async def test_verify_hf_ref_missing_when_revision_not_found(monkeypatch):
    _install_fake_client(monkeypatch, _FakeResponse(404))
    result = await external_refs.verify_hf_ref("model", "chrishayuk/nonexistent", "main", None)
    assert result.status == "missing"


async def test_verify_hf_ref_missing_when_size_falls_short_of_expected(monkeypatch):
    # The 2026-07-19 larql near-miss: HF had 2.6GB of a 36.5GB expected repo.
    _install_fake_client(
        monkeypatch, _FakeResponse(200, [{"type": "file", "path": "manifest.json", "size": 2_600_000_000}])
    )
    result = await external_refs.verify_hf_ref(
        "model", "chrishayuk/granite-4.1-30b-q4k-vindex", "main", 36_500_000_000
    )
    assert result.status == "missing"


async def test_verify_hf_ref_verified_when_size_meets_expected(monkeypatch):
    _install_fake_client(
        monkeypatch, _FakeResponse(200, [{"type": "file", "path": "weights.bin", "size": 10_000}])
    )
    result = await external_refs.verify_hf_ref("model", "chrishayuk/some-model", "main", 10_000)
    assert result.status == "verified"


async def test_verify_hf_ref_unverifiable_on_non_json_response(monkeypatch):
    _install_fake_client(monkeypatch, _FakeResponse(200, None))
    result = await external_refs.verify_hf_ref("model", "chrishayuk/some-model", "main", None)
    assert result.status == "unverifiable"


async def test_verify_hf_ref_sends_token_as_bearer_header_when_configured(monkeypatch):
    monkeypatch.setattr(type(settings), "huggingface_token", property(lambda self: "hf-secret"))
    fake_client_cls = _install_fake_client(monkeypatch, _FakeResponse(200, []))
    await external_refs.verify_hf_ref("model", "chrishayuk/some-model", "main", None)
    assert fake_client_cls.calls[0]["headers"]["Authorization"] == "Bearer hf-secret"


async def test_verify_hf_ref_unverifiable_on_network_error(monkeypatch):
    import httpx

    _install_fake_client(monkeypatch, httpx.ConnectError("boom"))
    result = await external_refs.verify_hf_ref("model", "chrishayuk/some-model", "main", None)
    assert result.status == "unverifiable"


async def test_verify_hf_ref_unverifiable_on_unexpected_status(monkeypatch):
    _install_fake_client(monkeypatch, _FakeResponse(500))
    result = await external_refs.verify_hf_ref("model", "chrishayuk/some-model", "main", None)
    assert result.status == "unverifiable"
