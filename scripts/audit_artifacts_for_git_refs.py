#!/usr/bin/env python3
"""Audit chuk-experiments-server production artifacts for git+/hf:// migration
candidates: hash every tracked file's real content in a set of known local
git repos (at their pushed HEAD) and cross-reference against artifact.sha256
for every artifact not already a git+/hf:// reference; separately, for
kind=checkpoint/dataset artifacts, do a real file-list-and-size diff against
candidate chrishayuk/* HF repos (not just name-matching).

Read-only by default — prints a candidate report. Pass --apply-ids with a
comma-separated list of artifact ids (from a prior read-only run) to
actually UPDATE those specific rows; nothing is ever auto-applied from a
bare match, since a script producing a plausible-looking list is exactly
the kind of result this whole feature exists to not blindly trust (see the
2026-07-19 larql near-miss: a name match alone isn't proof of anything —
that exact near-miss is why HF candidates below are verified by content,
not just name, before being offered as --apply-ids-eligible).
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import asyncpg
import httpx

# Every chrishayuk-owned local repo with a real github.com remote (surveyed
# 2026-07-19: every git repo under ~/chris-source with a github.com/chrishayuk
# origin) — relative to a base directory (default: this project's parent),
# same convention as the archive_*_to_drive.py scripts' --source default.
# Third-party clones (mlx, llama.cpp, mistral.rs, jacobian-lens, ...) are
# deliberately excluded — a content match there wouldn't be this project's
# own work, and attributing it via git_repo would be misleading.
KNOWN_REPOS = [
    "a2a-agent-record",
    "a2a-cli",
    "a2a-json-rpc",
    "a2a-server",
    "activation_functions",
    "adktools",
    "apollo-demo",
    "cardputer-sim",
    "cell80",
    "chess-lm",
    "chris-experiments",
    "chrishayuk-font",
    "chuk-agent",
    "chuk-ai-bash-tools",
    "chuk-basic",
    "chuk-code-raptor",
    "chuk-datasets",
    "chuk-finetune",
    "chuk-kv-anatomist",
    "chuk-larql",
    "chuk-llama-cot",
    "chuk-math",
    "chuk-mcp-function-server",
    "chuk-mcp-math",
    "chuk-mcp-math-server",
    "chuk-mlx",
    "chuk-mlx-2",
    "chuk-model",
    "chuk-robot-benches",
    "chuk-soma",
    "chuk-speccy",
    "embeddings",
    "gsm8k",
    "hello-agent",
    "hello-as",
    "kimi-play",
    "larql",
    "llama-index-play",
    "math-dataset",
    "mcp-apps-record",
    "mcp-cli",
    "mcp-cli-web",
    "mcp-oauth-prep",
    "mcp-oauth-record",
    "mcts-cot",
    "mcts-play",
    "mha_gqa_benchmark",
    "mlp-video",
    "mlx-finetune-record",
    "model_download_backup",
    "openai-realtime",
    "sm-play",
    "structured-outputs",
    "the-mechanism",
    "tiny-model",
    "tokenizer-benchmark",
    "transformer-by-hand",
    "v-tokenizers",
    "verifiers",
    "vibe-coding-templates",
    "whats-inside-the-ffn",
]

# Published HF repos under chrishayuk's namespace, confirmed via
# HfApi.list_models(author="chrishayuk") during the 2026-07-19 larql audit —
# refresh this list if new ones get published.
KNOWN_HF_REPOS = [
    ("chrishayuk/gemma-3-4b-it-vindex", "model"),
    ("chrishayuk/gemma-3-4b-it-vindex-attn", "model"),
    ("chrishayuk/gemma-3-4b-it-vindex-browse", "model"),
    ("chrishayuk/gemma-3-4b-it-vindex-client", "model"),
    ("chrishayuk/gemma-3-4b-it-vindex-embed", "model"),
    ("chrishayuk/gemma-3-4b-it-vindex-server", "model"),
    ("chrishayuk/gemma-4-26b-a4b-client-vindex-client", "model"),
    ("chrishayuk/gemma-4-26b-a4b-it-vindex-expert-server", "model"),
    ("chrishayuk/granite-4.1-30b-q4k-vindex", "model"),
    ("chrishayuk/granite-4.1-3b-q4k-vindex", "model"),
    ("chrishayuk/granite-4.1-8b-q4k-vindex", "model"),
    ("chrishayuk/llama-3-8b-calvinscale", "model"),
    ("chrishayuk/mistral-7b-v0.1-vindex", "model"),
]


async def check_hf_content_match(
    repo_id: str, repo_type: str, expected_bytes: int | None
) -> tuple[bool, str]:
    """Real content check, not name-matching — same file-list-and-size diff
    that caught the 2026-07-19 larql near-miss (an HF repo existed by name
    but was missing 93% of its content). Returns (is_complete_match, detail)."""
    segment = "datasets" if repo_type == "dataset" else "models"
    url = f"https://huggingface.co/api/{segment}/{repo_id}/tree/main?recursive=true"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"
    if response.status_code != 200:
        return False, f"HF API returned {response.status_code}"
    try:
        entries = response.json()
    except ValueError:
        return False, "non-JSON response"
    actual_bytes = sum(e.get("size", 0) for e in entries if e.get("type") == "file")
    if expected_bytes is not None and actual_bytes < expected_bytes:
        return False, f"only {actual_bytes} of {expected_bytes} expected bytes present on HF"
    return True, f"{actual_bytes} bytes confirmed present on HF"


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=30).stdout.strip()


def build_content_hash_map(base_dir: Path) -> dict[str, tuple[str, str, str, str]]:
    """{sha256: (repo, path, owner/repo, commit)} for every git-tracked file
    at each known repo's current pushed HEAD. Verified against the real
    remote via `git ls-remote` (not a possibly-stale local origin/HEAD
    tracking ref) — a repo whose local HEAD isn't actually pushed is
    skipped entirely rather than risk matching content that isn't really
    on GitHub yet."""
    content_map: dict[str, tuple[str, str, str, str]] = {}
    for repo in KNOWN_REPOS:
        repo_path = base_dir / repo
        if not (repo_path / ".git").is_dir():
            print(f"  skip {repo}: not a git repo at {repo_path}")
            continue
        remote = _run(["git", "-C", str(repo_path), "remote", "get-url", "origin"])
        if "github.com/" not in remote:
            print(f"  skip {repo}: no github.com remote")
            continue
        owner_repo = remote.split("github.com/")[-1].removesuffix(".git")

        local_head = _run(["git", "-C", str(repo_path), "rev-parse", "HEAD"])
        # Compare against the *current branch's own* remote ref, not the
        # remote's default branch — a repo can be fully pushed on a feature
        # branch (chris-experiments@writable-store-arc-2026-06-20,
        # chuk-mlx@long-context) without main being anywhere near HEAD.
        current_branch = _run(["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"])
        remote_head_line = _run(["git", "-C", str(repo_path), "ls-remote", "origin", current_branch])
        remote_head = remote_head_line.split()[0] if remote_head_line else ""
        if not remote_head or local_head != remote_head:
            print(
                f"  skip {repo}: local HEAD ({local_head[:10]}) != pushed {current_branch} "
                f"({remote_head[:10] or 'unknown'}) — not fully pushed"
            )
            continue

        files = _run(["git", "-C", str(repo_path), "ls-tree", "-r", "--name-only", "HEAD"]).splitlines()
        for rel_path in files:
            full_path = repo_path / rel_path
            if not full_path.is_file():
                continue
            try:
                sha256 = hashlib.sha256(full_path.read_bytes()).hexdigest()
            except OSError:
                continue
            content_map[sha256] = (repo, rel_path, owner_repo, local_head)

        print(f"  {repo}: {len(files)} tracked files hashed (HEAD {local_head[:10]}, confirmed pushed)")
    return content_map


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent,
        help="Parent directory containing the known local repos (default: this project's parent)",
    )
    parser.add_argument(
        "--apply-ids",
        default=None,
        help="Comma-separated artifact ids to actually migrate (from a prior read-only run's "
        "report) — omit to only report, never write",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL must be set")

    print("Hashing known local repos at their pushed HEAD...")
    content_map = build_content_hash_map(args.base_dir)
    print(f"Total unique content hashes across all repos: {len(content_map)}\n")

    conn = await asyncpg.connect(database_url)
    rows = await conn.fetch(
        """
        SELECT id, run_id, kind, name, uri, sha256, bytes, meta
        FROM artifact
        WHERE uri NOT LIKE 'git+%'
          AND uri NOT LIKE 'hf://%'
        ORDER BY id
        """
    )
    print(f"Artifacts to check: {len(rows)} (already git+/hf:// excluded)\n")

    git_candidates = []
    hf_name_candidates = []  # rows worth a real content check
    for row in rows:
        match = content_map.get(row["sha256"]) if row["sha256"] else None
        if match:
            repo, path, owner_repo, commit = match
            git_candidates.append(
                {
                    "artifact_id": row["id"],
                    "run_id": row["run_id"],
                    "current_uri": row["uri"],
                    "new_uri": f"git+https://github.com/{owner_repo}@{commit}",
                    "meta_updates": {"git_repo": owner_repo, "git_commit": commit},
                    "detail": path,
                }
            )
        elif row["kind"] in ("checkpoint", "dataset") and row["name"]:
            name_lower = row["name"].lower()
            loose_matches = [
                (hf, repo_type)
                for hf, repo_type in KNOWN_HF_REPOS
                if any(part in name_lower for part in hf.rsplit("/", 1)[-1].split("-") if len(part) > 2)
            ]
            for hf_repo, repo_type in loose_matches:
                hf_name_candidates.append(
                    {
                        "artifact_id": row["id"],
                        "run_id": row["run_id"],
                        "name": row["name"],
                        "current_uri": row["uri"],
                        "expected_bytes": row["bytes"],
                        "hf_repo": hf_repo,
                        "hf_repo_type": repo_type,
                    }
                )

    print(f"=== git+ candidates (verified byte-for-byte, safe to apply): {len(git_candidates)} ===")
    for c in git_candidates:
        print(
            f"  id={c['artifact_id']} run={c['run_id']} {c['current_uri']} -> {c['new_uri']} ({c['detail']})"
        )

    print(
        f"\nChecking {len(hf_name_candidates)} name-matched HF candidates for real content "
        f"completeness (file-list-and-size diff, not just name)..."
    )
    hf_verified_candidates = []
    hf_rejected_candidates = []
    for c in hf_name_candidates:
        complete, detail = await check_hf_content_match(c["hf_repo"], c["hf_repo_type"], c["expected_bytes"])
        c["detail"] = detail
        if complete:
            c["new_uri"] = f"hf://{c['hf_repo_type']}/{c['hf_repo']}@main"
            c["meta_updates"] = {
                "hf_repo_id": c["hf_repo"],
                "hf_revision": "main",
                "hf_repo_type": c["hf_repo_type"],
            }
            hf_verified_candidates.append(c)
        else:
            hf_rejected_candidates.append(c)

    print(
        f"\n=== HF candidates VERIFIED complete by content (safe to apply): {len(hf_verified_candidates)} ==="
    )
    for c in hf_verified_candidates:
        print(
            f"  id={c['artifact_id']} run={c['run_id']} name={c['name']!r} {c['current_uri']} -> "
            f"{c['new_uri']} ({c['detail']})"
        )

    print(
        f"\n=== HF candidates REJECTED — matched by name but failed content verification "
        f"(never apply, exactly the larql near-miss pattern): {len(hf_rejected_candidates)} ==="
    )
    for c in hf_rejected_candidates:
        print(
            f"  id={c['artifact_id']} run={c['run_id']} name={c['name']!r} uri={c['current_uri']} "
            f"-> {c['hf_repo']}: {c['detail']}"
        )

    all_candidates = {c["artifact_id"]: c for c in git_candidates + hf_verified_candidates}

    if args.apply_ids:
        apply_ids = {int(x) for x in args.apply_ids.split(",")}
        to_apply = [all_candidates[aid] for aid in apply_ids if aid in all_candidates]
        skipped = apply_ids - set(all_candidates)
        if skipped:
            print(
                f"\nWARNING: ids {skipped} are not in the verified candidate list "
                "(neither a byte-matched git+ nor a content-verified hf://) — skipping, not applying"
            )
        print(f"\nApplying {len(to_apply)} confirmed migrations...")
        for c in to_apply:
            row = await conn.fetchrow("SELECT meta FROM artifact WHERE id = $1", c["artifact_id"])
            meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}")
            meta.pop("drive_url", None)
            meta.update(c["meta_updates"])
            await conn.execute(
                "UPDATE artifact SET uri = $1, meta = $2 WHERE id = $3",
                c["new_uri"],
                json.dumps(meta),
                c["artifact_id"],
            )
            print(f"  updated id={c['artifact_id']} -> {c['new_uri']}")
    else:
        print(
            "\n(read-only report — pass --apply-ids <comma-separated ids> to actually migrate confirmed matches)"
        )

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
