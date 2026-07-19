"""git+/hf:// artifact reference URIs — build/parse helpers, plus a real
verify step that checks a registered reference still resolves.

Motivated directly by a 2026-07-19 disk-reclaim pass over `larql/output/`:
a vindex directory *looked* backed up on Hugging Face because a repo of the
same name existed on the Hub — but that repo only held 2.6GB of the local
directory's 36.5GB, missing the actual weight binaries entirely. Trusting
the name would have been a second silent-data-loss near-miss the same day.
verify_hf_ref below does the same file-list-and-size diff that caught it
by hand that day, as a real, on-demand, cached server feature instead of a
one-off script.

Only github.com is checked for git refs (every repo this project has seen
is GitHub-hosted) — any other host comes back `unverifiable`, not a false
`verified`. Both verify functions hit plain REST APIs via httpx (already a
dependency) rather than pulling in the GitHub/huggingface_hub SDKs.
"""

from http import HTTPStatus
from typing import Any, Literal, NamedTuple
from urllib.parse import urlparse

import httpx

from .constants import GIT_URI_PREFIXES, HF_URI_PREFIX

VerifyStatus = Literal["verified", "missing", "unverifiable"]


class VerifyResult(NamedTuple):
    status: VerifyStatus
    detail: str


def build_git_uri(owner: str, repo: str, commit: str) -> str:
    return f"git+https://github.com/{owner}/{repo}@{commit}"


def parse_git_uri(uri: str) -> tuple[str, str, str, str]:
    """Returns (host, owner, repo, commit)."""
    if not uri.startswith(GIT_URI_PREFIXES):
        raise ValueError(f"not a git+ artifact uri: {uri}")
    url, _, commit = uri.removeprefix("git+").rpartition("@")
    if not url or not commit:
        raise ValueError(f"git+ uri missing @commit: {uri}")
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"git+ uri missing /owner/repo path: {uri}")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    return parsed.netloc, owner, repo, commit


def build_hf_uri(repo_type: str, repo_id: str, revision: str) -> str:
    if repo_type not in ("model", "dataset"):
        raise ValueError(f"repo_type must be 'model' or 'dataset', got {repo_type!r}")
    return f"{HF_URI_PREFIX}{repo_type}/{repo_id}@{revision}"


def parse_hf_uri(uri: str) -> tuple[str, str, str]:
    """Returns (repo_type, repo_id, revision)."""
    if not uri.startswith(HF_URI_PREFIX):
        raise ValueError(f"not an hf:// artifact uri: {uri}")
    repo_type, _, rest = uri.removeprefix(HF_URI_PREFIX).partition("/")
    if repo_type not in ("model", "dataset"):
        raise ValueError(f"hf:// uri repo_type must be 'model' or 'dataset': {uri}")
    if "@" in rest:
        repo_id, _, revision = rest.rpartition("@")
    else:
        repo_id, revision = rest, "main"
    if not repo_id:
        raise ValueError(f"hf:// uri missing repo_id: {uri}")
    return repo_type, repo_id, revision


async def verify_git_ref(
    host: str, owner: str, repo: str, commit: str, token: str | None = None
) -> VerifyResult:
    if host != "github.com":
        return VerifyResult("unverifiable", f"only github.com git hosts are checked today, got {host!r}")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return VerifyResult("unverifiable", f"network error contacting GitHub: {exc}")
    if response.status_code == HTTPStatus.OK:
        return VerifyResult("verified", f"commit {commit} exists on {owner}/{repo}")
    if response.status_code == HTTPStatus.NOT_FOUND:
        return VerifyResult("missing", f"commit {commit} not found on {owner}/{repo}")
    return VerifyResult(
        "unverifiable", f"GitHub API returned {response.status_code} for {owner}/{repo}@{commit}"
    )


async def verify_hf_ref(
    repo_type: str, repo_id: str, revision: str, expected_bytes: int | None, token: str | None = None
) -> VerifyResult:
    segment = "datasets" if repo_type == "dataset" else "models"
    url = f"https://huggingface.co/api/{segment}/{repo_id}/tree/{revision}?recursive=true"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return VerifyResult("unverifiable", f"network error contacting Hugging Face: {exc}")
    if response.status_code == HTTPStatus.NOT_FOUND:
        return VerifyResult("missing", f"{repo_type} {repo_id}@{revision} not found on Hugging Face")
    if response.status_code != HTTPStatus.OK:
        return VerifyResult(
            "unverifiable", f"Hugging Face API returned {response.status_code} for {repo_id}@{revision}"
        )
    try:
        entries: list[dict[str, Any]] = response.json()
    except ValueError:
        return VerifyResult("unverifiable", "Hugging Face API returned a non-JSON response")
    actual_bytes = sum(entry.get("size", 0) for entry in entries if entry.get("type") == "file")
    if expected_bytes is not None and actual_bytes < expected_bytes:
        return VerifyResult(
            "missing",
            f"only {actual_bytes} of {expected_bytes} expected bytes present on Hugging Face for "
            f"{repo_id}@{revision} — the revision exists but is missing real content, the exact "
            "failure mode a name-only check misses",
        )
    return VerifyResult("verified", f"{actual_bytes} bytes present on Hugging Face for {repo_id}@{revision}")
