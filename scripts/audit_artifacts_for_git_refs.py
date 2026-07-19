#!/usr/bin/env python3
"""Audit chuk-experiments-server production artifacts for git+ migration
candidates: hash every tracked file's real content in a set of known local
git repos (at their pushed HEAD) and cross-reference against artifact.sha256
for every artifact not already a git+/hf:// reference.

Read-only by default — prints a candidate report. Pass --apply-ids with a
comma-separated list of artifact ids (from a prior read-only run) to
actually UPDATE those specific rows; nothing is ever auto-applied from a
bare match, since a script producing a plausible-looking list is exactly
the kind of result this whole feature exists to not blindly trust (see the
2026-07-19 larql near-miss: a name match alone isn't proof of anything).

HF-checkpoint matching is NOT attempted here: an HF repo's content-
addressing (its own blob oids) doesn't share this project's sha256 space,
so there's no honest automatic match the way there is for git — instead,
kind=checkpoint/dataset artifacts with no git match, whose `name` loosely
resembles a published chrishayuk/* HF repo, are listed separately for a
human to check by hand, the same way the original larql/granite audit was.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import asyncpg

# Every local repo confirmed this session to have a real github.com remote —
# relative to a base directory (default: this project's parent), same
# convention as the archive_*_to_drive.py scripts' --source default.
KNOWN_REPOS = [
    "chris-experiments",
    "chuk-mlx",
    "tiny-model",
    "larql",
    "cell80",
    "chuk-speccy",
    "cardputer-sim",
    "chuk-soma",
    "chuk-robot-benches",
]

# Published HF repos under chrishayuk's namespace, confirmed via
# HfApi.list_models(author="chrishayuk") during the 2026-07-19 larql audit —
# refresh this list if new ones get published.
KNOWN_HF_REPOS = [
    "chrishayuk/gemma-3-4b-it-vindex",
    "chrishayuk/gemma-3-4b-it-vindex-attn",
    "chrishayuk/gemma-3-4b-it-vindex-browse",
    "chrishayuk/gemma-3-4b-it-vindex-client",
    "chrishayuk/gemma-3-4b-it-vindex-embed",
    "chrishayuk/gemma-3-4b-it-vindex-server",
    "chrishayuk/gemma-4-26b-a4b-client-vindex-client",
    "chrishayuk/gemma-4-26b-a4b-it-vindex-expert-server",
    "chrishayuk/granite-4.1-30b-q4k-vindex",
    "chrishayuk/granite-4.1-3b-q4k-vindex",
    "chrishayuk/granite-4.1-8b-q4k-vindex",
    "chrishayuk/llama-3-8b-calvinscale",
    "chrishayuk/mistral-7b-v0.1-vindex",
]


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
        SELECT id, run_id, kind, name, uri, sha256, meta
        FROM artifact
        WHERE sha256 IS NOT NULL
          AND uri NOT LIKE 'git+%'
          AND uri NOT LIKE 'hf://%'
        ORDER BY id
        """
    )
    print(f"Artifacts to check: {len(rows)} (already git+/hf:// or no sha256 excluded)\n")

    git_candidates = []
    hf_review_candidates = []
    for row in rows:
        match = content_map.get(row["sha256"])
        if match:
            repo, path, owner_repo, commit = match
            git_candidates.append(
                {
                    "artifact_id": row["id"],
                    "run_id": row["run_id"],
                    "current_uri": row["uri"],
                    "matched_repo": repo,
                    "matched_path": path,
                    "matched_owner_repo": owner_repo,
                    "matched_commit": commit,
                }
            )
        elif row["kind"] in ("checkpoint", "dataset") and row["name"]:
            name_lower = row["name"].lower()
            loose_matches = [
                hf
                for hf in KNOWN_HF_REPOS
                if any(part in name_lower for part in hf.rsplit("/", 1)[-1].split("-") if len(part) > 2)
            ]
            if loose_matches:
                hf_review_candidates.append(
                    {
                        "artifact_id": row["id"],
                        "run_id": row["run_id"],
                        "name": row["name"],
                        "current_uri": row["uri"],
                        "possible_hf_repos": loose_matches,
                    }
                )

    print(f"=== git+ candidates (verified byte-for-byte, safe to apply): {len(git_candidates)} ===")
    for c in git_candidates:
        print(
            f"  id={c['artifact_id']} run={c['run_id']} {c['current_uri']} -> "
            f"git+https://github.com/{c['matched_owner_repo']}@{c['matched_commit']} ({c['matched_path']})"
        )

    print(
        f"\n=== HF checkpoint/dataset candidates (NAME MATCH ONLY — needs manual review, "
        f"never auto-applied): {len(hf_review_candidates)} ==="
    )
    for c in hf_review_candidates:
        print(
            f"  id={c['artifact_id']} run={c['run_id']} name={c['name']!r} uri={c['current_uri']} "
            f"-> maybe one of {c['possible_hf_repos']}"
        )

    if args.apply_ids:
        apply_ids = {int(x) for x in args.apply_ids.split(",")}
        to_apply = [c for c in git_candidates if c["artifact_id"] in apply_ids]
        skipped = apply_ids - {c["artifact_id"] for c in to_apply}
        if skipped:
            print(
                f"\nWARNING: ids {skipped} are not in the verified git_candidates list — skipping, not applying"
            )
        print(f"\nApplying {len(to_apply)} confirmed git+ migrations...")
        for c in to_apply:
            row = await conn.fetchrow("SELECT meta FROM artifact WHERE id = $1", c["artifact_id"])
            meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}")
            meta.pop("drive_url", None)
            meta["git_repo"] = c["matched_owner_repo"]
            meta["git_commit"] = c["matched_commit"]
            new_uri = f"git+https://github.com/{c['matched_owner_repo']}@{c['matched_commit']}"
            await conn.execute(
                "UPDATE artifact SET uri = $1, meta = $2 WHERE id = $3",
                new_uri,
                json.dumps(meta),
                c["artifact_id"],
            )
            print(f"  updated id={c['artifact_id']}")
    else:
        print(
            "\n(read-only report — pass --apply-ids <comma-separated ids> to actually migrate confirmed matches)"
        )

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
