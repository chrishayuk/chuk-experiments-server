#!/usr/bin/env python3
"""Seed chuk-experiments-server from chuk-mlx/experiments/.

Unlike chris-experiments, there's no INDEX.md register here — each directory
under experiments/ IS one experiment, documented by its own EXPERIMENT.md
(primary write-up) plus optionally RESULTS.md/README.md. There's no numeric
id and no explicit status/hypothesis field to mine, so per-experiment
tagging is sparser than migrate_chris_experiments.py produces — the full
finding lives in the concatenated write-up body, not in structured metadata.

All directories are treated as historical/completed, same reasoning as
migrate_chris_experiments.py: it already happened, whatever shape the result
took.

Talks to a running server over the REST API, same as the other migration
scripts (and reuses their shared helpers).
"""

import argparse
import re
import sys
from pathlib import Path

import httpx

from _migrate_common import create_experiment as post_experiment
from _migrate_common import create_run as post_run
from _migrate_common import slugify

_PROGRAMME_SLUG = "chuk-mlx"
_WRITEUP_FILENAMES = ("EXPERIMENT.md", "RESULTS.md", "README.md")
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_TAKEAWAY_HEADING_RE = re.compile(r"^#+\s*(key takeaway|conclusions?|summary)\s*$", re.IGNORECASE | re.MULTILINE)


def humanize(dirname: str) -> str:
    return dirname.replace("_", " ").replace("-", " ").title()


def extract_title(text: str, fallback: str) -> str:
    match = _TITLE_RE.search(text)
    return match.group(1).strip() if match else fallback


def extract_takeaway(text: str) -> str | None:
    """Best-effort: the paragraph right after a 'Key Takeaway'/'Conclusion(s)'/
    'Summary' heading, if one exists. Returns None rather than fabricating a
    summary when the write-up doesn't have one."""
    match = _TAKEAWAY_HEADING_RE.search(text)
    if not match:
        return None
    lines: list[str] = []
    for line in text[match.end() :].lstrip("\n").splitlines():
        if line.startswith("#"):
            break
        if not line.strip() and lines:
            break
        lines.append(line)
    paragraph = "\n".join(lines).strip()
    return paragraph or None


def build_writeup(exp_dir: Path) -> tuple[str, str]:
    title = humanize(exp_dir.name)
    parts = []
    for filename in _WRITEUP_FILENAMES:
        path = exp_dir / filename
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        if filename == "EXPERIMENT.md":
            title = extract_title(text, title)
        parts.append(f"<!-- {filename} -->\n\n{text}")
    body = "\n\n---\n\n".join(parts)
    return title, body or f"# {title}\n\n(no EXPERIMENT.md/RESULTS.md/README.md found in this directory)"


def discover_experiments(root: Path) -> list[Path]:
    experiment_dirs = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "__")):
            continue
        if any((child / filename).exists() for filename in _WRITEUP_FILENAMES):
            experiment_dirs.append(child)
    return experiment_dirs


def run_migration(root: Path, base_url: str, api_key: str | None, dry_run: bool) -> None:
    experiment_dirs = discover_experiments(root)
    print(f"Found {len(experiment_dirs)} experiment directories under {root}")

    if dry_run:
        for exp_dir in experiment_dirs:
            title, body = build_writeup(exp_dir)
            takeaway = extract_takeaway(body)
            print(f"  {exp_dir.name:35s} title={title!r} writeup_len={len(body)} takeaway={'yes' if takeaway else 'no'}")
        return

    if not api_key:
        sys.exit("--api-key is required unless --dry-run")

    client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    created = 0

    for exp_dir in experiment_dirs:
        slug = f"{_PROGRAMME_SLUG}-{slugify(exp_dir.name)}"
        title, body = build_writeup(exp_dir)
        takeaway = extract_takeaway(body)
        path = f"experiments/{exp_dir.name}"

        experiment = post_experiment(
            client,
            {
                "programme": _PROGRAMME_SLUG,
                "slug": slug,
                "title": title,
                "design": {"path": path},
                "tags": [_PROGRAMME_SLUG, "historical"],
                "status": "completed",
            },
        )
        if experiment is None:
            continue

        client.post(f"/v1/experiments/{slug}/writeups", json={"body_md": body})

        run = post_run(
            client, slug, {"slug": "historical", "backend": "other", "config": {"path": path}, "status": "completed"}
        )
        if run and takeaway:
            client.post(f"/v1/runs/{run['id']}/results", json={"name": "summary", "notes": takeaway})

        created += 1

    print(f"Done: created {created}/{len(experiment_dirs)} experiments")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=Path("../chuk-mlx/experiments"))
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", help="Bearer key with write scope (required unless --dry-run)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_migration(args.source, args.base_url, args.api_key, args.dry_run)


if __name__ == "__main__":
    main()
