#!/usr/bin/env python3
"""Seed chuk-experiments-server from Chris's existing chris-experiments/INDEX.md.

INDEX.md is a hand-maintained "## Programme (id range)" -> "### id — title"
-> {Path, Status, Summary, Result} register that already has almost exactly
the shape of programme/experiment/writeup/result. This is a best-effort
STRUCTURAL migration, not a semantic one:

  - Every migrated experiment/run gets `status=completed` — it's history,
    it already happened, regardless of what its raw INDEX.md status says
    ("blocked", "incomplete", "scaffolded", ...). That raw status isn't
    thrown away though: it's preserved verbatim in `design.raw_status`, AND
    any of a fixed set of markers it contains (superseded, abandoned,
    blocked, incomplete, scaffolded, active, negative, falsified, retracted,
    conditional) become tags, so `list_experiments(tags=["abandoned"])`
    still finds them.
  - The one seeded result's `verdict` is a coarse "does the status line
    still call this live" signal (inconclusive for superseded/abandoned,
    else pass) — not a read of the actual finding. Many "pass"-verdict
    experiments report a negative/falsified result in their prose; that
    nuance lives in `notes` and the write-up body (pulled from the
    experiment's README.md when one exists at its `Path`).

Talks to a running server over the REST API (not the DB directly) — it's
just an API client, same as anything else with a write-scoped key.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from _migrate_common import create_experiment as post_experiment
from _migrate_common import create_run as post_run
from _migrate_common import slugify

HEADING_RE = re.compile(r"^##\s+(.+)$")
EXPERIMENT_RE = re.compile(r"^###\s+(.+)$")
FIELD_RE = re.compile(r"^-\s+\*\*(\w+):\*\*\s*(.*)$")

#: Every migrated entry is history — it already happened, whatever its raw
#: INDEX.md status says ("blocked", "incomplete", "scaffolded", ...) — so
#: experiment/run status is always 'completed', full stop. The raw status is
#: preserved in `design.raw_status` and still drives the *verdict* below,
#: since "abandoned"/"superseded" is still useful signal about the finding.
_MIGRATED_EXPERIMENT_STATUS = "completed"
_MIGRATED_RUN_STATUS = "completed"


@dataclass
class ParsedExperiment:
    programme_slug: str
    programme_name: str
    programme_uncommitted: bool
    heading: str
    path: str | None
    raw_status: str | None
    summary: str | None
    result: str | None


def programme_slug_and_name(heading: str) -> tuple[str, str, bool]:
    uncommitted = "uncommitted" in heading.lower()
    name = re.split(r"[—(]", heading, maxsplit=1)[0].strip()
    return slugify(name), name, uncommitted


def experiment_id_and_title(heading: str) -> tuple[str, str]:
    if "—" in heading:
        id_part, title = heading.split("—", 1)
        return id_part.strip(), title.strip()
    return "", heading.strip()


#: Markers pulled out of the raw INDEX.md status line and kept as tags, since
#: collapsing `status` to a flat 'completed' would otherwise throw this
#: information away. Checked as substrings, in order, against the lowercased
#: raw status — every match becomes a tag (an entry can be e.g. both
#: "abandoned" and "superseded").
_STATUS_TAG_MARKERS = (
    "superseded",
    "abandoned",
    "blocked",
    "incomplete",
    "scaffolded",
    "active",
    "negative",
    "falsified",
    "retracted",
    "conditional",
)


def status_tags(raw_status: str | None) -> list[str]:
    if not raw_status:
        return []
    lowered = raw_status.lower()
    return [marker for marker in _STATUS_TAG_MARKERS if marker in lowered]


def map_verdict(raw_status: str | None) -> str:
    """Coarse verdict from the raw INDEX.md status text — not a read of the
    actual finding (many 'completed'-in-our-schema experiments report a
    negative/falsified result in their prose; that nuance lives in `notes`),
    just whether the experiment's own status line still calls it live."""
    if not raw_status:
        return "n/a"
    lowered = raw_status.lower()
    if "superseded" in lowered or "abandoned" in lowered:
        return "inconclusive"
    return "pass"


def parse_index(text: str) -> list[ParsedExperiment]:
    lines = text.splitlines()
    experiments: list[ParsedExperiment] = []
    programme: tuple[str, str, bool] | None = None
    i = 0
    while i < len(lines):
        line = lines[i]

        heading_match = HEADING_RE.match(line)
        if heading_match:
            programme = programme_slug_and_name(heading_match.group(1))
            i += 1
            continue

        exp_match = EXPERIMENT_RE.match(line)
        if exp_match and programme:
            heading = exp_match.group(1).strip()
            fields: dict[str, str] = {}
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if HEADING_RE.match(nxt) or EXPERIMENT_RE.match(nxt) or nxt.strip() == "---":
                    break
                field_match = FIELD_RE.match(nxt.strip())
                if field_match:
                    fields[field_match.group(1).strip().lower()] = field_match.group(2).strip()
                j += 1

            if "path" in fields:  # only genuine experiment blocks carry a Path bullet
                experiments.append(
                    ParsedExperiment(
                        programme_slug=programme[0],
                        programme_name=programme[1],
                        programme_uncommitted=programme[2],
                        heading=heading,
                        path=fields["path"].strip("`") or None,
                        raw_status=fields.get("status"),
                        summary=fields.get("summary"),
                        result=fields.get("result"),
                    )
                )
            i = j
            continue

        i += 1
    return experiments


def build_slug(programme_slug: str, id_part: str, title: str, seen: set[str]) -> str:
    parts = [programme_slug, slugify(id_part), slugify(title)[:40]]
    base = "-".join(p for p in parts if p)
    slug, n = base, 2
    while slug in seen:
        slug = f"{base}-{n}"
        n += 1
    seen.add(slug)
    return slug


def build_writeup(root: Path, exp: ParsedExperiment) -> str:
    header_lines = [
        f"**Path:** `{exp.path}`" if exp.path else None,
        f"**Status (raw):** {exp.raw_status}" if exp.raw_status else None,
        f"**Summary:** {exp.summary}" if exp.summary else None,
        f"**Result:** {exp.result}" if exp.result else None,
    ]
    header = "\n\n".join(line for line in header_lines if line)

    readme_text = None
    if exp.path:
        readme_path = root / exp.path / "README.md"
        if readme_path.exists():
            readme_text = readme_path.read_text(errors="replace")

    if readme_text:
        return f"{header}\n\n---\n\n{readme_text}" if header else readme_text
    return header or exp.heading


def build_tags(exp: "ParsedExperiment") -> list[str]:
    tags = [exp.programme_slug, "historical", *status_tags(exp.raw_status)]
    if exp.programme_uncommitted:
        tags.append("uncommitted")
    if "(parallel)" in exp.heading.lower():
        tags.append("parallel")
    return tags


def run_migration(source: Path, base_url: str, api_key: str | None, dry_run: bool) -> None:
    text = source.read_text()
    root = source.resolve().parent
    experiments = parse_index(text)
    programmes = {e.programme_slug for e in experiments}
    print(f"Parsed {len(experiments)} experiments across {len(programmes)} programmes: {sorted(programmes)}")

    if dry_run:
        for exp in experiments:
            print(f"  [{exp.programme_slug:20s}] {exp.heading}  tags={build_tags(exp)}")
        return

    if not api_key:
        sys.exit("--api-key is required unless --dry-run")

    client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    seen_slugs: set[str] = set()
    created = 0

    for exp in experiments:
        id_part, title = experiment_id_and_title(exp.heading)
        slug = build_slug(exp.programme_slug, id_part, title, seen_slugs)

        experiment = post_experiment(
            client,
            {
                "programme": exp.programme_slug,
                "slug": slug,
                "title": title,
                "hypothesis": exp.summary,
                "design": {"path": exp.path, "raw_status": exp.raw_status},
                "tags": build_tags(exp),
                "status": _MIGRATED_EXPERIMENT_STATUS,
            },
        )
        if experiment is None:
            continue

        client.post(f"/v1/experiments/{slug}/writeups", json={"body_md": build_writeup(root, exp)})

        run = post_run(
            client,
            slug,
            {
                "slug": "historical",
                "backend": "other",
                "config": {"path": exp.path},
                "status": _MIGRATED_RUN_STATUS,
            },
        )
        if run and exp.result:
            client.post(
                f"/v1/runs/{run['id']}/results",
                json={"name": "summary", "verdict": map_verdict(exp.raw_status), "notes": exp.result},
            )

        created += 1
        if created % 20 == 0:
            print(f"... {created}/{len(experiments)}")

    print(f"Done: created {created}/{len(experiments)} experiments")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=Path("../chris-experiments/INDEX.md"))
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", help="Bearer key with write scope (required unless --dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print without calling the API")
    args = parser.parse_args()
    run_migration(args.source, args.base_url, args.api_key, args.dry_run)


if __name__ == "__main__":
    main()
