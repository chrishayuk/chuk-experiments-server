from typing import Any

from ..constants import DEFAULT_LIST_LIMIT, DEFAULT_SEARCH_LIMIT, MAX_LIST_LIMIT
from ..server import mcp
from ._shared import _api_request, _drop_body_html, _listing, _query_params


@mcp.tool
async def get_index(programme: str | None = None, limit: int = MAX_LIST_LIMIT, offset: int = 0) -> Any:
    """A compact, paginated catalogue: slug, title, tags, status, hypothesis
    (truncated to ~200 chars — full text is in get_experiment), and headline
    metric per experiment.

    NOT "the whole catalogue in one call" once a project has more than a
    couple hundred experiments — check `total` in the response against
    `count`/your `limit` and page with `offset` if they differ. For
    structured filtering (status, tags, conclusion/next-action needed)
    rather than a compact skim, use list_experiments instead — this tool's
    value is the hypothesis+headline-metric projection, not filter power.
    Try this (or list_experiments) before search_experiments for browsing;
    try 2-3 phrasings of search_experiments before concluding something
    doesn't exist by search alone.

    Args:
        programme: Only experiments in this programme (e.g. "cn")
        limit: Maximum rows to return this page
        offset: Rows to skip (for paging past `limit`)
    """
    params = _query_params(programme=programme, limit=limit, offset=offset)
    body = await _api_request("GET", "/v1/index", params=params)
    if not isinstance(body, dict) or "results" not in body:
        return body
    return _listing(
        body["results"],
        "0 experiments" + (f" in programme {programme!r}" if programme else "") + " exist.",
        total=body.get("total"),
    )


@mcp.tool
async def list_experiments(
    programme: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    q: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> Any:
    """Browse experiments, optionally filtered by programme slug, status, tags, or full-text query.

    Args:
        programme: Programme slug to filter by (e.g. "cn")
        status: Experiment status to filter by (draft/planned/running/completed/abandoned/superseded)
        tags: Only experiments with at least one of these tags
        q: Free-text search over title/hypothesis/write-up
        limit: Maximum rows to return
    """
    params = _query_params(programme=programme, status=status, q=q, limit=limit)
    if tags:
        params["tag"] = tags
    return await _api_request("GET", "/v1/experiments", params=params)


@mcp.tool
async def get_experiment(slug: str) -> Any:
    """Fetch the full record for one experiment: hypothesis, design, latest write-up, and its runs.

    Args:
        slug: Experiment slug (e.g. "cn-7")
    """
    return _drop_body_html(await _api_request("GET", f"/v1/experiments/{slug}"))


@mcp.tool
async def search_experiments(
    query: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> Any:
    """Full-text search over titles/hypotheses/write-ups, combinable with structured filters.

    Search is lexical (Postgres FTS), not semantic — a paraphrase of an
    experiment's exact topic can return nothing even though the experiment
    exists. Zero hits doesn't mean "not found"; it means "not found under
    these words" — try 2-3 different phrasings before concluding something
    doesn't exist, and consider list_experiments/get_index (browse instead
    of search) if you're not sure what terms it would use.

    Args:
        query: Free-text search query
        filters: Optional dict — programme, status, tags (list), config_key +
            config_value (matches a JSONB key on any of the experiment's
            runs), metric + metric_op + metric_value (matches a result value
            on any run, e.g. {"metric": "gsm8k_acc", "metric_op": "gt",
            "metric_value": 0.4}; metric_op is one of eq/ne/gt/gte/lt/lte)
        limit: Maximum rows to return
    """
    filters = filters or {}
    params = _query_params(
        q=query, programme=filters.get("programme"), status=filters.get("status"), limit=limit
    )
    if filters.get("tags"):
        params["tag"] = filters["tags"]
    if filters.get("config_key") and filters.get("config_value") is not None:
        params[f"config.{filters['config_key']}"] = filters["config_value"]
    if filters.get("metric") and filters.get("metric_op") and filters.get("metric_value") is not None:
        params["metric"] = filters["metric"]
        params["op"] = filters["metric_op"]
        params["value"] = filters["metric_value"]
    hits = await _api_request("GET", "/v1/search", params=params)
    if not isinstance(hits, list):
        return hits
    return _listing(
        hits,
        f"0 experiments matched {query!r} via lexical search (not semantic) with "
        "the given filters — try a different phrasing, or list_experiments/"
        "get_index to browse instead of searching.",
    )


@mcp.tool
async def create_experiment(
    programme: str,
    title: str,
    slug: str | None = None,
    hypothesis: str | None = None,
    design: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Any:
    """Register a new planned experiment.

    Args:
        programme: Programme slug this experiment belongs to
        title: Short human-readable title
        slug: Unique experiment slug (e.g. "cn-11") — auto-generated
            (e.g. "EXP-20260718-160217-00397") when omitted
        hypothesis: A single, falsifiable claim in plain language — what you
            expect to observe and why, not an inventory of every
            component/artifact the run touches (that belongs in `design`,
            not here). One to three sentences, written so someone with zero
            context on this run's internal jargon/acronyms can still tell
            what's being tested and what result would prove it wrong.

            Bad (a real case this caused): "A fully pinned, model-free
            harness — target sets, MSI frame battery + canonicalizer
            (Python+Rust), parity corpus, blind D4 call set, shadow-lane
            configs, five-way census taxonomy, TEG uncertainty policy,
            panel-adjudication table — committed before any candidate
            trains eliminates post-hoc discretion..." — this lists what was
            built, not a claim; nothing in it is falsifiable, and a reader
            has to decode eight acronyms before reaching any actual idea.

            Good: "Freezing every scoring decision (target sets,
            calibration corpus, adjudication rules) before training any
            tokenizer candidate will prevent the kind of post-hoc
            rule-bending that inflated the previous version's reported win
            margin." — one mechanism, one prediction, no undefined jargon.
        design: Model/dataset/params/arms as a JSON object — this is where
            harness components, acronyms, and configuration inventories
            belong, not in `hypothesis`
        tags: Freeform labels for filtering later (list_experiments/
            search_experiments both filter on these) — set them now rather
            than via a follow-up update_experiment_status call
    """
    body = {
        "programme": programme,
        "slug": slug,
        "title": title,
        "hypothesis": hypothesis,
        "design": design or {},
        "tags": tags or [],
    }
    return await _api_request("POST", "/v1/experiments", json=body)


@mcp.tool
async def update_experiment_status(slug: str, status: str, tags: list[str] | None = None) -> Any:
    """Update an experiment's own lifecycle status — separate from any of
    its runs' statuses, and nothing flips it automatically: an experiment
    stays "planned" forever unless something calls this, even while its
    runs are actively running or completed. Call this when real work
    starts (-> "running") and when it wraps up (-> "completed"), the same
    way set_run_status keeps a run's own status current.

    Args:
        slug: Experiment slug (e.g. "cn-7")
        status: draft/planned/running/completed/abandoned/superseded
        tags: Optional replacement tag list (omit to leave tags unchanged)
    """
    body: dict[str, Any] = {"status": status}
    if tags is not None:
        body["tags"] = tags
    return await _api_request("PATCH", f"/v1/experiments/{slug}", json=body)


@mcp.tool
async def append_writeup(slug: str, body_md: str) -> Any:
    """Append a new write-up version to an experiment (author is the calling API key's identity).

    Args:
        slug: Experiment slug
        body_md: Write-up body in markdown, written for a reader who wasn't
            in the room. Lead with the verdict in one plain-language
            sentence before any internal jargon or acronyms — a reader
            should know whether this closed positive, negative, or
            inconclusive from the first line, not have to parse the whole
            thing to find out. Define any abbreviation the first time it's
            used. Separate what was done, what was found, and what it
            means into distinct sections rather than one dense paragraph —
            the same failure mode that makes a hypothesis unreadable
            (jargon crammed together with no claim) makes a write-up
            unreadable too.
    """
    return _drop_body_html(
        await _api_request("POST", f"/v1/experiments/{slug}/writeups", json={"body_md": body_md})
    )


@mcp.tool
async def record_experiment_conclusion(
    slug: str, conclusion: str | None = None, next_action: str | None = None
) -> Any:
    """Record what an experiment established and what should happen next —
    separate from update_experiment_status (that's lifecycle: draft/planned/
    running/completed/... this is narrative: what did we learn, what now).
    Call this once real analysis is done, alongside or shortly after your
    final update_experiment_status(status="completed") / write-up.

    Args:
        slug: Experiment slug (e.g. "cn-7")
        conclusion: What this experiment established, in plain language.
            Open with the verdict relative to the hypothesis — supported,
            refuted, mixed, or inconclusive — before any detail, then say
            why in a sentence or two. Bad: restating the method ("ran the
            sweep across 5 configs"). Good: "Refuted: increasing vocabulary
            size alone did not close the Rust family-held-out gap — the
            gap tracks a specific missing token class, not vocabulary
            coverage in general." Omit to leave the existing conclusion
            unchanged.
        next_action: What should happen next, or why work stopped here —
            concrete enough that reading it in six months tells you
            exactly what to do. Bad: "investigate further." Good: "Try
            TOK-14: matched vocabulary with/without structural numeral
            encoding, to isolate whether the gap is numeral-specific" or
            "Park — superseded by cn-9's cleaner replication" or "Closed,
            refuted; no follow-up planned." Omit to leave unchanged.
    """
    body: dict[str, Any] = {}
    if conclusion is not None:
        body["conclusion"] = conclusion
    if next_action is not None:
        body["next_action"] = next_action
    return await _api_request("PATCH", f"/v1/experiments/{slug}", json=body)
