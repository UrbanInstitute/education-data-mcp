import asyncio
import html
import re
from typing import TypedDict
from urllib.parse import urlencode

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from edp_mcp import __version__
from edp_mcp.api_client import EdpClient, api_error_message
from edp_mcp.constants import (
    DOCUMENTATION_URL,
    MAX_FETCH_ROWS,
    MAX_RESPONSE_CHARS,
    PUBLIC_BASE_URL,
)
from edp_mcp.formatting import (
    format_data,
    format_dataset_header,
    format_endpoints,
    format_entity_matches,
    format_preview,
    format_provenance_footer,
    format_ranked,
    format_summary,
    format_summary_recipe,
    format_too_large,
    format_values,
    format_variables,
    records_to_csv,
    strip_code_prefix,
)
from edp_mcp.metadata import (
    MetadataCache,
    _clean_path,
    _path_segments,
    summary_by_pool,
    summary_path_for,
    summary_var_pool,
)
from edp_mcp.runtime import add_health_route, serve

mcp = MCPServer(
    name="Education Data Portal",
    version=__version__,
    website_url=DOCUMENTATION_URL,
    # Policy that applies across tools lives here rather than in each tool's
    # description: this is sent once, a description is re-sent with every tool
    # listing. Descriptions carry only what is specific to their own tool.
    #
    # HARD BUDGET: keep this under ~1,800 characters. MCP clients truncate
    # server instructions — the Claude Code CLI cuts at 2,048 and says nothing,
    # and the Messages API connector appears to drop them entirely, delivering
    # only tool descriptions. Anything that MUST reach the model belongs in a
    # tool description or in the result text, both of which arrive whole.
    instructions=(
        "You are connected to the Urban Institute's Education Data Portal, which "
        "harmonises federal education data (CCD, CRDC, IPEDS, EdFacts, SAIPE, "
        "College Scorecard, MEPS, PSEO, and others) for schools, school districts, "
        "and colleges.\n\n"

        "CHOOSING A TOOL\n"
        "- Totals, averages or counts by group — get_summary. It aggregates in "
        "the API and groups by year automatically, so ONE call covers a whole "
        "time series. Its path, var and by come from describe_dataset, which "
        "prints the exact call — they are not guessable from the data path.\n"
        "- A named school, district or college — resolve_entity first, to turn "
        "the name into an ID to filter on.\n"
        "- Specific rows — get_data, always with fields=.\n"
        '- "Largest / smallest N" — get_data with ordering=-<field> for '
        "highest first, ordering=<field> for lowest.\n"
        "- Unfamiliar with the data — search_datasets, then describe_dataset.\n\n"

        "ALWAYS PASS fields= TO get_data. These tables are 50-96 columns wide; "
        "requesting only the columns you need is typically an 8-17x reduction "
        "and is usually the difference between an answer and a refusal.\n\n"

        "FILTERS: only fields marked [FILTER] in describe_dataset work. The API "
        "silently ignores an unsupported filter and returns UNFILTERED data, so "
        "check before assuming a filter applied. Filter by state using FIPS "
        "codes — see lookup_codes(format_name='fips').\n\n"

        "RESULTS ARE COMPLETE OR REFUSED, never silently truncated, so counting "
        "and averaging over what you receive is valid. A refusal reports the "
        "true size and how to narrow it — follow it rather than retrying the "
        "same query.\n\n"

        "PASS THE SOURCE ON. Every data result ends with a provenance block "
        "naming the federal source and carrying a preformatted Cite string. The "
        "data is open under ODC-By, which asks for attribution wherever figures "
        "are reported — so name the source with the numbers, and reproduce that "
        "Cite string rather than composing one of your own."
    ),
)

add_health_route(mcp, "edp-mcp")

# Every tool here reads a public API and never writes, so the annotations are
# the same on all of them: safe to auto-approve, and network-backed.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

# Valid statistics for summary endpoints
VALID_STATS = {"sum", "count", "avg", "min", "max", "variance", "stddev", "median"}

_client = EdpClient()
_cache = MetadataCache(_client)


# Rows shown by preview=True. Enough to see the shape and the range of values,
# few enough that no one mistakes it for a population.
PREVIEW_ROWS = 25

# Rows returned for a server-ranked (ordering=) query. "Top N" questions are
# asked about a handful; past that the caller wants the whole set, which is what
# an unranked query already gives when it fits.
RANKED_ROWS = 25


def _projected_chars(
    records: list[dict], total_count: int, fields: list[str] | None
) -> int:
    """Rendered size of the FULL result, scaled up from the rows actually fetched.

    Only used to describe a refusal whose result set was too big to fetch whole.
    Every size *decision* is made on a real rendered body, so an estimate is never
    what stands between a caller and their data.
    """
    if not records:
        return 0
    rendered = len(records_to_csv(records, fields))
    return round(rendered * max(total_count, len(records)) / len(records))


def _rows_that_fit(
    records: list[dict], fields: list[str] | None, cap: int
) -> list[dict]:
    """The first `cap` rows, or fewer when the table is wide enough that `cap` of
    them would still blow the budget (IPEDS is 96 columns).

    Halves until it fits rather than estimating a per-row cost: measuring the
    rendered body is exact where an estimate is not, and over a couple of dozen
    rows the repeated render costs nothing.
    """
    shown = records[:cap]
    while len(shown) > 1 and len(records_to_csv(shown, fields)) > MAX_RESPONSE_CHARS:
        shown = shown[: len(shown) // 2]
    return shown


# Scope filters ordered coarse → fine, with what each narrows the query to. A
# description of None marks a key that pins the query to a SINGLE entity: those
# are never suggested, because "restrict to one school" answers a different
# question than the one that was asked. They still count as scope, so a query
# already that narrow falls through to the generic advice instead.
_NARROWING_KEYS = [
    ("fips", "one state (see lookup_codes(format_name='fips'))"),
    ("leaid", "one district (see resolve_entity)"),
    ("ncessch", None),
    ("unitid", None),
]


def _narrowing_hint(params: dict, variables: list[dict]) -> str:
    """Suggest the next scope filter FINER than the ones the query already uses.

    Skipping past what is already set is what keeps the advice from handing back
    the caller's own filter: a query filtered to one district must not be told to
    add a state, which would not shrink it at all.
    """
    filterable = {
        v.get("variable")
        for v in variables
        if str(v.get("is_filter", "0")) == "1"
    }
    remaining = _NARROWING_KEYS
    for i, (key, _) in enumerate(_NARROWING_KEYS):
        if key in params:
            remaining = _NARROWING_KEYS[i + 1:]
    for key, description in remaining:
        if description and key in filterable:
            return f"add {key}=… to restrict to {description}"
    return (
        "add a filter from describe_dataset(filterable_only=True), or use the "
        "options below if the query is already as narrow as it goes"
    )


def _summary_error(path: str, var: str, by: str, error: Exception) -> str:
    """Recovery-oriented message for a summary request that failed upstream.

    The portal usually explains the failure itself, in one precise sentence.
    Passing that through is worth more than any guess assembled here: it names
    the one wrong argument, where a guess has to hedge across the endpoint, the
    var and the by, and sends the caller off to re-check all three.
    """
    reported = api_error_message(error)
    if reported:
        return (
            f"The Education Data Portal rejected this summary: {reported}\n\n"
            f"Everything else about the query was accepted, so fix just that "
            f"argument and retry. describe_dataset('{path}') lists the valid "
            f"var and by values."
        )
    return (
        f"Could not compute a summary for '{path}'. The portal returned an error "
        f"without explanation, which usually means the aggregation backend is "
        f"unavailable rather than that the query is wrong. Retry once; if it "
        f"persists, use get_data with fields= and aggregate the rows yourself. "
        f"(Underlying error: {error})"
    )


async def _unknown_summary_path_error(path: str) -> str:
    """A summary path that matches no endpoint, answered with the right one.

    The overwhelmingly common cause is a DATA path used as a summary path — a
    template straight from search_datasets, or one with its placeholders filled
    in. Both identify a real dataset, so the correct summary path is a lookup
    rather than a guess, and the reply can be the answer instead of a menu.
    """
    endpoint, _ = await _cache.match_path(path)
    if endpoint is not None:
        correct = summary_path_for(endpoint.get("endpoint_url", ""))
        return (
            f"'{path}' is a data path, not a summary path. Summaries are served "
            f"from their own endpoints, which take no year and no {{placeholders}}.\n\n"
            f"Use path='{correct}' — it covers this dataset, and every year of it "
            f"in one call."
        )

    available = await _cache.summary_paths()
    wanted = _path_segments(path)
    scored = []
    for candidate in available:
        segments = _path_segments(candidate)
        shared = 0
        for want, got in zip(segments, wanted, strict=False):
            if want.lower() != got.lower():
                break
            shared += 1
        if shared:
            scored.append((-shared, candidate))
    scored.sort()
    suggestions = [c for _, c in scored[:8]]
    body = (
        f"No summary endpoint at '{path}'. Summary paths take no year and no "
        f"{{placeholders}}."
    )
    if suggestions:
        listed = "\n".join(f"  {s}" for s in suggestions)
        return f"{body} Closest available:\n\n{listed}"
    return (
        f"{body} Call search_datasets to find the dataset, then pass its path to "
        f"describe_dataset — the summary path is listed there."
    )


def _summary_argument_error(
    kind: str, value: str, pool: list[str], joined: list[str], path: str
) -> str:
    """Reject a var/by the backend is certain to reject, naming the alternatives.

    Worth catching here rather than upstream: the backend loads its own metadata
    before it validates, so a wrong argument costs a full round trip to learn
    something the varlist already on hand could have said instantly.
    """
    lines = [f"'{value}' is not a valid '{kind}' for {path}."]
    if kind == "var":
        lines.append(
            "var must be a numeric, non-filter variable — the thing being "
            "measured, never a grouping."
        )
    else:
        lines.append("by must be a variable of this dataset — the grouping.")
    if pool:
        lines.append(f"\nValid {kind} values: {', '.join(pool)}")
    if joined:
        lines.append(
            f"Also valid as by, via the pre-joined directory: {', '.join(joined[:25])}"
            + (" …" if len(joined) > 25 else "")
        )
    return "\n".join(lines)


async def _build_footer(
    endpoint: dict,
    source: str,
    api_url: str,
    query_years: str,
) -> str:
    """Assemble the provenance footer from live metadata + verified constants.

    Pulls source label (api-sources) and version (api-changes, max release_date);
    omits anything it can't resolve. The LLM never touches this — it returns a
    finished string.

    Both lookups are cached for the process lifetime and fetched concurrently.
    Serially they would be two round trips on every single data response, for
    values that change only on a versioned EDP release.
    """
    source_code = endpoint.get("class_name") or source
    source_label, version = await asyncio.gather(
        _cache.get_source_label(source_code),
        _cache.get_version(),
    )
    years_available = endpoint.get("years_available")
    if years_available:
        years_available = html.unescape(years_available)

    return format_provenance_footer(
        source_code=source_code,
        source_label=source_label,
        years_available=years_available,
        query_years=query_years,
        api_url=api_url,
        version=version,
    )


@mcp.tool(title="Search datasets", annotations=READ_ONLY)
async def search_datasets(
    level: str | None = None,
    source: str | None = None,
    topic: str | None = None,
    search: str | None = None,
) -> str:
    """Find datasets in the Education Data Portal. Returns each one's path
    template, description and years available; pass a template to
    describe_dataset for its variables and its summary call. All arguments are
    optional filters.

    NOT COVERED by this portal at all: NAEP scores (see the NAEP Data
    Explorer), teacher salaries (BLS), private K-12 (limited; CRDC covers
    some), and curriculum data. Say so rather than searching repeatedly.

    Args:
        level: "schools", "school-districts", or "college-university"
        source: "ccd", "ipeds", "crdc", "edfacts", "saipe", "scorecard", …
        topic: "enrollment", "directory", "finance", "discipline", …
        search: Words matched per-token against dataset paths and descriptions,
            not as a phrase — so drop specific terms like grade numbers
            ("8th") if a query returns nothing, since datasets rarely spell
            those out verbatim.
    """
    endpoints = await _cache.filter_endpoints(
        level=level, source=source, topic=topic, search=search
    )
    return format_endpoints(endpoints)


@mcp.tool(title="Describe dataset", annotations=READ_ONLY)
async def describe_dataset(path: str, filterable_only: bool = False) -> str:
    """Inspect one dataset: every variable with its definition, data type,
    coded-value format, and whether it is filterable. Also returns the years
    covered, the source description, and equivalent R/Stata code.

    This is where the [FILTER] marks come from — filtering on anything else
    returns UNFILTERED data rather than an error.

    Args:
        path: Dataset path, template or filled in — both resolve to the same
            dataset: "schools/ccd/enrollment/{year}/{grade}/race/" or
            "schools/ccd/enrollment/2022/grade-99/race/"
        filterable_only: Show only the variables usable as filters
    """
    endpoint, _ = await _cache.match_path(path)
    if endpoint is None:
        # A summary path names the same dataset by a different route, and is what
        # every get_summary error tells the caller to describe. Resolving it here
        # keeps that advice from dead-ending.
        summary_matches = await _cache.match_summary_path(path)
        if summary_matches:
            endpoint = summary_matches[0]
        else:
            return await _unknown_path_error(path)

    variables, source_info, join_variables = await asyncio.gather(
        _cache.get_endpoint_varlist(endpoint["endpoint_id"]),
        _cache.get_source_info(endpoint.get("class_name")),
        _cache.summary_join_varlist(endpoint),
    )
    return (
        format_dataset_header(endpoint, source_info)
        + format_summary_recipe(endpoint, variables, join_variables)
        + format_variables(variables, filterable_only=filterable_only)
    )


@mcp.tool(title="Get data records", annotations=READ_ONLY)
async def get_data(
    path: str,
    filters: str | None = None,
    fields: str | None = None,
    add_labels: bool = True,
    preview: bool = False,
) -> str:
    """Fetch raw data records from one dataset path. For totals or averages
    across groups use get_summary instead — it aggregates server-side.

    Results are COMPLETE or refused, never truncated, so counting and averaging
    over the rows returned is valid. A query matching over 10,000 rows is refused
    with its true size and how to narrow it. When one is too big, aggregate with
    get_summary, narrow to a state or district, or hand the user the bulk CSV
    link from the refusal or the R/Stata snippet from describe_dataset — for
    whole-country or long multi-year analysis those beat paging through calls.

    COST: this hits a live API and a broad query can take 30s+. Filter before
    fetching rather than issuing many wide calls in parallel.

    Args:
        path: A dataset path from search_datasets with every {placeholder}
            filled in — "schools/ccd/enrollment/2022/grade-99/race/". One year
            per call; a leading "/api/v1/" is optional.
        filters: "fips=11&charter=1". Only fields marked [FILTER] in
            describe_dataset work; others are rejected here rather than silently
            returning unfiltered data. Also takes "ordering=" to rank
            server-side: "ordering=-enrollment" largest first, "ordering=enrollment"
            smallest. Ranking sorts the whole result before paging, so a ranked
            query answers where an unranked one is refused as too large.
        fields: Comma-separated columns — "ncessch,school_name,enrollment".
            ALWAYS PASS THIS. These tables are 50-96 columns wide; requesting
            only what you need is typically an 8-17x reduction and is usually
            the difference between an answer and a refusal.
        add_labels: Decode coded values to labels (default True). Decoding is
            per variable, so meanings are FIELD-SPECIFIC — trust the decoded
            label over any assumption about what a raw code means.
        preview: Return a small labelled SAMPLE rather than a complete result.
            The rows are the API's first N by ID, NOT a random sample — never
            count, rank or average over them.
    """
    endpoint, path_params = await _cache.match_path(path)
    if endpoint is None:
        return await _unknown_path_error(path)

    # `endpoint` is guarded for None above, but a missing endpoint_id is a
    # different failure: it would flow as None into five calls that all expect an
    # int and surface somewhere unrelated. Upstream metadata always carries it,
    # so this is a guard against upstream changing, not a case seen in practice.
    endpoint_id = endpoint.get("endpoint_id")
    if endpoint_id is None:
        return (
            f"Dataset '{path}' has no endpoint_id in the API metadata, so its "
            "variables cannot be looked up. This is an upstream metadata problem, "
            "not a problem with the query."
        )
    variables = await _cache.get_endpoint_varlist(endpoint_id)

    # Reject a year the dataset doesn't cover before firing — the API answers an
    # out-of-range year with a 500, which is indistinguishable from a real fault.
    year = path_params.get("year")
    year_error = _check_year(endpoint, year)
    if year_error:
        return year_error

    try:
        params = _parse_filters(filters)
    except FilterFormatError as e:
        return str(e)
    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
    if field_list:
        # EDP supports `fields=` server-side, so this cuts bandwidth as well as
        # output size.
        params["fields"] = ",".join(field_list)

    filter_error = _check_filters(params, variables, path)
    if filter_error:
        return filter_error

    column_error = _check_columns(field_list, params.get("ordering"), variables, path)
    if column_error:
        return column_error

    request_path = _clean_path(path) + "/"
    try:
        records, total_count = await _client.fetch_data(
            request_path, params=params or None, max_records=MAX_FETCH_ROWS
        )
    except (httpx.HTTPStatusError, httpx.TransportError) as e:
        return (
            f"Could not fetch '{request_path}'. The path matched a known dataset, "
            f"so this is an upstream failure or an invalid filter value in "
            f"'{filters}'. Call describe_dataset(path='{path}') to see valid "
            f"filters. (Underlying error: {e})"
        )

    # Preview: the one sanctioned exception to complete-or-refuse. Restores "let me
    # see what this looks like" for tables too large to return, without weakening
    # the guarantee — the rows are labelled a non-representative sample, not a
    # result.
    if preview:
        shown = _rows_that_fit(records, field_list, PREVIEW_ROWS)
        if add_labels and shown:
            shown = await _apply_labels(endpoint_id, shown)
        return format_preview(shown, total_count, fields=field_list)

    # `ordering` sorts server-side, BEFORE the API pages, so the rows that came
    # back are the true top of the full result rather than an arbitrary slice.
    # That is the one thing complete-or-refuse is guarding against, so a ranked
    # query answers where an unranked one refuses.
    ranked = params.get("ordering")
    labels_applied = False
    api_url = f"{PUBLIC_BASE_URL}{request_path}" + (f"?{urlencode(params)}" if params else "")

    async def too_large(chars: int) -> str:
        if ranked:
            shown = _rows_that_fit(records, field_list, RANKED_ROWS)
            if add_labels and shown and not labels_applied:
                shown = await _apply_labels(endpoint_id, shown)
            # A ranked result is an answer, so it is cited like one — and the
            # echoed URL carries ordering=, which is what makes the ranking
            # reproducible rather than something the caller has to take on trust.
            return format_ranked(
                shown, total_count, ranked, fields=field_list,
                footer=await _build_footer(
                    endpoint, endpoint.get("class_name", ""), api_url, year or ""
                ),
            )
        return format_too_large(
            what=_clean_path(path),
            total_count=total_count,
            estimated_chars=chars,
            narrowing_hint=_narrowing_hint(params, variables),
            download_urls=await _cache.get_download_urls(
                endpoint_id, [int(year)] if year and year.isdigit() else None
            ),
        )

    # Complete-or-refuse. The API capped the fetch, so the full set can never be
    # rendered — its size is the one that has to be projected.
    if len(records) < total_count:
        return await too_large(_projected_chars(records, total_count, field_list))

    if add_labels and records:
        records = await _apply_labels(endpoint_id, records)
        labels_applied = True

    # Decided on the real rendered body rather than an estimate of it: everything
    # that drives the size is already known here — the labels are applied (they
    # are longer than the codes they replace, "California" for 6) and the CSV is
    # built — so measuring it is both exact and simpler than predicting it.
    body = format_data(records, total_count, fields=field_list)
    if len(body) > MAX_RESPONSE_CHARS:
        return await too_large(len(body))

    footer = await _build_footer(
        endpoint, endpoint.get("class_name", ""), api_url, year or "",
    )
    return f"{body}\n\n{footer}" if footer else body


@mcp.tool(title="Summarize data", annotations=READ_ONLY)
async def get_summary(
    path: str,
    var: str,
    stat: str,
    by: str,
    filters: str | None = None,
) -> str:
    """Aggregate a dataset server-side: counts, sums, averages by group. Use
    this rather than get_data whenever the question is about totals ("how many
    schools per state", "total enrollment by race").

    EVERY year comes back in one call — results are grouped by year on top of
    `by`, so a trend needs one call, never one per year. Filter years only to
    narrow a large result. Results are COMPLETE or refused. The API cannot rank
    aggregates, and refuses groupings that produce too many groups (by=leaid
    nationally, for instance).

    `var` and `by` are drawn from two different pools — describe_dataset lists
    both for any dataset. Guessing costs a slow round trip; reading them does not.

    COST: this hits a live API and a broad aggregation can take 30s+.

    NO COUNTY AGGREGATION for schools or districts. Several datasets return
    `county_code` as a column, but it is neither filterable nor groupable, so
    county totals cannot be computed here — fetch the rows with get_data and
    aggregate them yourself, or say the portal does not support it. Do not
    retry with county in `by`.

    Args:
        path: The SUMMARY path — no year, no {placeholders}, e.g.
            "schools/ccd/enrollment". describe_dataset prints the right one for
            any dataset. Some differ from the data path
            ("college-university/ipeds/fall-enrollment/race").
        var: The measure to aggregate — a numeric, non-filter variable
        stat: sum, count, avg, min, max, median, stddev, or variance
        by: Comma-separated groupings — "fips", "fips,race". May also use
            variables from the source's directory ("school_level", "sector").
        filters: "fips=6" — omit year to get every year; "year=2018,2019,2020"
            to restrict the range
    """
    if stat.lower() not in VALID_STATS:
        return f"Invalid stat '{stat}'. Valid options: {', '.join(sorted(VALID_STATS))}."

    base = _clean_path(path)
    try:
        filter_dict = _parse_filters(filters)
    except FilterFormatError as e:
        return str(e)

    # get_data allows `ordering`, so it has to be refused explicitly here rather
    # than by _check_filters: the API 400s on ordering= for /summaries/.
    if "ordering" in filter_dict:
        return (
            "The API cannot rank aggregates — ordering= returns 400 on "
            "/summaries/. Remove it and sort the rows yourself: summary results "
            "are complete, so the ranking is yours to do. (get_data does support "
            "ordering= for ranking raw records.)"
        )

    # Validate against the whole family of templates sharing this summary path
    # (enrollment by grade, by grade+race, …): they aggregate through one
    # /summaries/ endpoint, so a field valid on any sibling is valid here.
    endpoints = await _cache.match_summary_path(base)
    if not endpoints:
        # An unroutable summary path returns a bare 500 that names nothing, so
        # this has to be caught here — upstream has no answer to pass through.
        return await _unknown_summary_path_error(base)

    endpoint = endpoints[0]
    varlists = await asyncio.gather(*(
        _cache.get_endpoint_varlist(e["endpoint_id"]) for e in endpoints
    ))
    variables = [v for varlist in varlists for v in varlist]
    # Summary tables are built pre-joined against their source's directory, so
    # these are legitimate groupings and filters even though they belong to a
    # different endpoint. Checking without them rejects valid queries.
    join_variables = await _cache.summary_join_varlist(endpoint)

    var_pool = summary_var_pool(variables)
    if var not in {v.get("variable") for v in variables} or var not in var_pool:
        return _summary_argument_error("var", var, var_pool, [], base)

    by_fields = [b.strip() for b in by.split(",") if b.strip()]
    known = {str(v.get("variable")) for v in variables + join_variables}
    by_pool = summary_by_pool(variables)
    join_by_pool = [b for b in summary_by_pool(join_variables) if b not in by_pool]
    for field in by_fields:
        # Only reject what the backend certainly rejects. It accepts any variable
        # of the table as a grouping, not just the filterable ones, so the pools
        # above steer without narrowing what is allowed.
        if field != "year" and field not in known:
            return _summary_argument_error("by", field, by_pool, join_by_pool, base)
    if var in by_fields:
        return (
            f"var and by cannot both be '{var}'. var is the measure being "
            f"aggregated; by is what it is broken out by. Valid by values: "
            f"{', '.join(by_pool)}"
        )

    # The same silently-unfiltered-data trap get_data guards, and worse here:
    # an aggregate that quietly lost its filter still looks like a valid table.
    filter_error = _check_filters(filter_dict, variables + join_variables, base)
    if filter_error:
        return filter_error

    year_filter = filter_dict.get("year")
    if year_filter and "," not in year_filter:
        # Accepted if ANY sibling covers it — coverage differs across variants,
        # and a false rejection is worse than an upstream error the caller can
        # read. A comma list is left alone rather than guessed at.
        year_errors = [_check_year(e, year_filter) for e in endpoints]
        if all(year_errors):
            # `all()` proves every entry is non-None, but only to a reader —
            # taking the first non-None entry says the same thing in a form
            # the type checker can follow. Identical result.
            return next(e for e in year_errors if e is not None)

    try:
        results, total_count = await _client.fetch_summary(
            path=base, var=var, stat=stat.lower(), by=by,
            filters=filter_dict or None,
        )
    except httpx.HTTPStatusError as e:
        # 413 is the API refusing an over-large grouping — a size problem, not a
        # schema problem. Saying "var/by is not valid" sends the caller off to
        # re-check a schema that was fine.
        if e.response.status_code == 413:
            return (
                f"GROUPING TOO FINE — the API refused to compute this summary "
                f"because by='{by}' produces too many groups.\n\n"
                f"Add a filter (e.g. filters='fips=6') to restrict the scope, or "
                f"group by a coarser field. Grouping by an entity ID such as "
                f"leaid or ncessch nationally will always exceed the limit."
            )
        return _summary_error(base, var, by, e)
    except httpx.TransportError as e:
        return _summary_error(base, var, by, e)

    def refuse(chars: int) -> str:
        # Year is the cheapest lever on a summary and the one a caller is least
        # likely to reach for, precisely because they never asked for every year:
        # the endpoint returns them all by default, so an unfiltered query
        # carries decades of rows nobody wanted.
        year_lever = (
            ""
            if filter_dict.get("year")
            else ", or add filters='year=…' — every year is included by default"
        )
        return format_too_large(
            what=f"{stat.upper()}({var}) grouped by {by}",
            total_count=total_count,
            estimated_chars=chars,
            narrowing_hint=(
                "group by fewer or coarser fields, or add a filter such as "
                f"filters='fips=6' to restrict the scope{year_lever}"
            ),
        )

    # Complete-or-refuse. Only the set too big to fetch whole needs projecting.
    if len(results) < total_count:
        return refuse(_projected_chars(results, total_count, None))

    # Decode coded grouping fields (fips, grade, ...) using the same per-variable
    # metadata get_data uses, so grade=-1 reads "Pre-K", not "Missing".
    if results and endpoint.get("endpoint_id") is not None:
        results = await _apply_labels(endpoint["endpoint_id"], results)

    body = format_summary(results, var, stat, by, filter_dict or None)
    if len(body) > MAX_RESPONSE_CHARS:
        return refuse(len(body))

    # A single-year summary is nearly always a caller who does not know the
    # whole series costs the same one call. Stating the coverage alongside the
    # result is what stops the year-at-a-time loop, since it arrives at the
    # moment the next call would be issued.
    if year_filter and "," not in year_filter:
        covered = html.unescape(endpoint.get("years_available") or "").strip()
        span = f" This dataset covers {covered}." if covered else ""
        body += (
            f"\n\nFiltered to year={year_filter}.{span} Summaries return every "
            f"year in one call — drop the year filter for the full series at no "
            f"extra cost."
        )

    summary_params = {"var": var, "stat": stat.lower(), "by": by}
    summary_params.update(filter_dict)
    footer = await _build_footer(
        endpoint, endpoint.get("class_name", ""),
        f"{PUBLIC_BASE_URL}{base}/summaries/?{urlencode(summary_params)}",
        filter_dict.get("year", ""),
    )
    return f"{body}\n\n{footer}" if footer else body


@mcp.tool(title="Look up coded values", annotations=READ_ONLY)
async def lookup_codes(
    format_name: str,
    codes: str | None = None,
) -> str:
    """Look up the code-to-label mapping for a coded variable — mainly to find a
    filter value for get_data ("California" -> fips=6).

    Args:
        format_name: The "Format" shown for a variable in describe_dataset —
            "fips", "race", "sex", "school_level", "charter", …
        codes: Comma-separated codes to look up — "6,48". Omit for all values.
    """
    values = await _cache.get_values(format_name)

    parsed_codes = None
    if codes:
        try:
            parsed_codes = [int(c.strip()) for c in codes.split(",")]
        except ValueError:
            return f"Invalid codes '{codes}'. Please provide comma-separated integers."

    return format_values(values, format_name, parsed_codes)


# Entity type configurations for resolve_entity
# Directory dataset per entity type, with the columns a name search actually
# reads. Requesting only those turns an 11.7 MB scan of a state's directory into
# a few hundred KB — the API supports `fields=` server-side.
#
# TypedDict rather than a bare dict: the values are heterogeneous (str, bool,
# str | None), so an untyped literal widens every lookup to `object` and the
# call sites below stop being checkable at all.
class _EntityConfig(TypedDict):
    path: str
    name_field: str
    id_field: str
    needs_state: bool
    level_field: str | None


_ENTITY_CONFIGS: dict[str, _EntityConfig] = {
    "school": {
        "path": "schools/ccd/directory",
        "name_field": "school_name",
        "id_field": "ncessch",
        # ~100K rows nationally, and names repeat across states — a lookup
        # without a state is a guess, so ask instead.
        "needs_state": True,
        # Grade level is matched as noise, so it has to be RETURNED instead:
        # "Lincoln Elementary" and "Lincoln High" in one city are otherwise
        # indistinguishable in the output, and city is the only other signal.
        "level_field": "school_level",
    },
    "district": {
        "path": "school-districts/ccd/directory",
        "name_field": "lea_name",
        "id_field": "leaid",
        "needs_state": True,
        # Districts span grade levels; there is no equivalent column.
        "level_field": None,
    },
    "college": {
        "path": "college-university/ipeds/directory",
        "name_field": "inst_name",
        "id_field": "unitid",
        # ~7K rows in one page and institution names are near-unique.
        "needs_state": False,
        "level_field": None,
    },
}

# Structural words carrying no distinguishing signal, dropped before matching so
# they can neither be required nor block a match. Grounded in the actual CCD
# directory vocabulary rather than invented.
_ENTITY_NOISE = {
    "school", "schools", "sch", "academy", "center", "campus", "charter",
    "program", "the", "of", "at", "for", "and", "public", "community",
    "elementary", "elem", "el", "primary", "pri", "middle", "ms", "high", "hs",
    "junior", "jr", "senior", "sr", "intermediate", "int",
    "isd", "cisd", "usd", "sd", "esd", "independent", "consolidated",
    "unified", "district", "county",
}


def _entity_tokens(name: str) -> set[str]:
    """Distinctive words of an entity name, for matching.

    Punctuation dropped, CCD's "H S" folded away with the other structural
    words, stray single letters removed. Grade-level words are noise here rather
    than a ranking signal: scoring candidates by level takes a pile of
    hand-tuned heuristics and can still rank the wrong school first, whereas
    returning more candidates and letting the caller disambiguate on city and
    level cannot be quietly wrong.
    """
    text = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return {
        t for t in text.split()
        if t not in _ENTITY_NOISE and not (len(t) == 1 and t.isalpha())
    }


def _entity_matches(query: str, candidate: str) -> bool:
    """True if `candidate` is a plausible match for `query`.

    Either a plain substring hit, or every distinctive word of the query appears
    in the candidate — which is what bridges natural phrasings to CCD's
    abbreviations ("Lakewood Elementary" -> "LAKEWOOD EL").
    """
    if query.lower() in (candidate or "").lower():
        return True
    wanted = _entity_tokens(query)
    return bool(wanted) and wanted <= _entity_tokens(candidate)


@mcp.tool(title="Resolve entity name to ID", annotations=READ_ONLY)
async def resolve_entity(
    name: str,
    entity_type: str,
    fips: int | None = None,
    max_results: int = 15,
) -> str:
    """Resolve a school/district/college NAME to the ID used in get_data filters.

    Returns CANDIDATES with their city and level, not one authoritative answer —
    names repeat, so the right row is the one whose city and level fit what the
    user meant. Type words ("Elementary", "High", "ISD") are ignored when
    matching, which is what lets natural phrasings reach the directory's
    abbreviations ("Lakewood Elementary" finds "LAKEWOOD EL").

    Schools and districts require `fips`: without it this asks which state rather
    than guessing, since "Homestead High" exists in CA, WI, IN and FL.

    COST: downloads a whole state's directory, so the first call for a state
    often takes 10-30s and later ones are cached. Resolve names in the same
    state together.

    Args:
        name: Name to search for; distinctive words matter most.
        entity_type: "school", "district", or "college"
        fips: State FIPS code (6 = California). Required for schools and
            districts. See lookup_codes(format_name="fips").
        max_results: Maximum candidates to return (default 15)
    """
    config = _ENTITY_CONFIGS.get(entity_type)
    if config is None:
        return f"Invalid entity_type '{entity_type}'. Use 'school', 'district', or 'college'."

    if fips is None and config["needs_state"]:
        return (
            f"Which state? A {entity_type} name is not unique nationally — "
            f"'{name}' may exist in several states, and picking the wrong one "
            f"silently returns the wrong entity.\n\n"
            f"ASK THE USER which state they mean, then re-run with that state's "
            f"fips (see lookup_codes(format_name='fips'))."
        )

    # Latest year the directory covers, so IDs are current.
    endpoint = next(
        (e for e in await _cache.get_endpoints()
         if _clean_path(e.get("endpoint_url", "")).startswith(config["path"])),
        None,
    )
    if endpoint is None:
        return f"Could not find the {entity_type} directory dataset."
    latest_year = _parse_latest_year(endpoint.get("years_available", ""))
    if latest_year is None:
        return f"Could not determine the latest year for the {entity_type} directory."

    name_field, id_field = config["name_field"], config["id_field"]
    level_field = config["level_field"]
    columns = [id_field, name_field, "city_location", "fips"]
    if level_field:
        columns.append(level_field)
    try:
        # Cached per (dataset, year, state): resolving several names in one state
        # is the normal case, and this scan is the server's most expensive fetch.
        records, total_count = await _cache.get_directory(
            config["path"], latest_year, columns, fips,
        )
    except (httpx.HTTPStatusError, httpx.TransportError) as e:
        return (
            f"Could not search the {entity_type} directory. This may be a "
            f"transient API error or timeout — narrowing with fips (e.g. fips=6) "
            f"makes the search faster and more reliable. (Underlying error: {e})"
        )

    matched = [r for r in records if _entity_matches(name, r.get(name_field, "") or "")]
    shown = matched[:max_results]

    # A partial scan that reads as a complete one is how the wrong entity gets
    # reported with full confidence, so any gap travels with the result.
    notes = []
    if len(records) < total_count:
        notes.append(
            "INCOMPLETE SCAN — part of the directory could not be read, so an "
            "absence here is not evidence the entity does not exist. Re-run with "
            "fips=<state> to search one state reliably."
        )
    if len(matched) > max_results:
        notes.append(
            f"Showing {len(shown)} of {len(matched)} candidates — narrow with fips "
            f"or a more specific name to see the rest."
        )
    if len(shown) > 1:
        notes.append(
            "Multiple candidates — pick the one whose city and level match the user's "
            "intent; don't assume the first is correct."
        )

    if not shown:
        scope = f" in fips={fips}" if fips else ""
        miss = (
            f"No {entity_type} found matching '{name}'{scope}. This matches on the "
            f"distinctive words of the name, not a search engine — check spelling "
            f"and try a shorter, more distinctive part of the name."
        )
        return f"{miss}\n\n{notes[0]}" if notes else miss

    # school_level arrives as an integer code; decoded it reads "High" instead of
    # "3", which is the whole point of returning it.
    if level_field:
        shown = await _apply_labels(endpoint["endpoint_id"], shown)

    return format_entity_matches(
        shown, entity_type, name_field, id_field, columns, note=" ".join(notes)
    )


class FilterFormatError(ValueError):
    """A malformed filter string. The message is written for the caller."""


def _parse_filters(filters: str | None) -> dict:
    """Parse a 'key=val&key2=val2' filter string into a dict. Empty if None.

    A pair that is not key=value raises rather than being skipped. Skipping it
    sent the request upstream WITHOUT that filter and returned an unfiltered
    result the caller reads as filtered — the same silent wrongness that
    unsupported filter KEYS are rejected to prevent.
    """
    params: dict[str, str] = {}
    if not filters:
        return params
    for pair in filters.split("&"):
        if not pair.strip():
            continue
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise FilterFormatError(
                f"Invalid filter '{pair.strip()}'. Filters are key=value pairs "
                f'joined by "&" — e.g. "fips=6&charter=1". Nothing was requested, '
                f"because a dropped filter would have returned unfiltered data."
            )
        params[key.strip()] = value.strip()
    return params


def _check_filters(params: dict, variables: list[dict], path: str) -> str | None:
    """Reject filters the API would silently ignore.

    Checked against the FILTERABLE variables, not every variable: the API answers
    a filter on a non-filterable column with HTTP 200 and UNFILTERED data —
    `?enrollment=500` returns all 10,416 California schools — so accepting any
    known variable name waves through exactly the failure this exists to prevent.
    """
    if not params or not variables:
        return None
    # str(): the trailing guard already drops falsy names, but only the coercion
    # makes that visible downstream to sorted() and join().
    filterable_set = {
        str(v["variable"]) for v in variables
        if str(v.get("is_filter", "0")) == "1" and v.get("variable")
    }
    filterable = sorted(filterable_set)
    # `fields` and `ordering` are real query params on data endpoints but are not
    # variables, so they would otherwise be rejected as unknown filter keys.
    unknown = [
        k for k in params
        if k not in filterable_set
        and k not in ("fields", "ordering")
    ]
    if not unknown:
        return None
    known = {v.get("variable") for v in variables}
    not_filterable = [k for k in unknown if k in known]
    detail = ""
    if not_filterable:
        verb = "is a variable" if len(not_filterable) == 1 else "are variables"
        detail = (
            f"{', '.join(not_filterable)} {verb} in this dataset but not filterable. "
        )
    return (
        f"Cannot filter on: {', '.join(unknown)}. {detail}The API ignores filters "
        f"it does not support and returns UNFILTERED data, so this query would "
        f"silently return everything. Valid filter fields: {', '.join(filterable)}. "
        f"Call describe_dataset for '{path}' to see them."
    )


def _check_columns(
    field_list: list[str] | None,
    ordering: str | None,
    variables: list[dict],
    path: str,
) -> str | None:
    """Reject `fields=` and `ordering=` columns the dataset does not have.

    Neither is rejected upstream — both are silently ignored, the same trap
    unsupported filter keys fall into. An unknown fields= column comes back as an
    empty column. An unknown ordering= column is worse: the API returns 200 with
    rows in ID order, so a typo turns a ranked answer into an arbitrary prefix
    still labelled "genuinely the highest". Verified against the live API on
    2026-07-28.
    """
    known = {v.get("variable") for v in variables if v.get("variable")}
    if not known:
        return None

    problems = []
    for column in field_list or []:
        if column not in known:
            problems.append(f"fields={column}")
    if ordering and ordering.lstrip("-") not in known:
        problems.append(f"ordering={ordering.lstrip('-')}")
    if not problems:
        return None

    return (
        f"Unknown column(s): {', '.join(problems)}. The API ignores these rather "
        f"than reporting them — an unknown fields= column returns blank, and an "
        f"unknown ordering= column returns UNRANKED rows that would still be "
        f"presented as a ranking. Call describe_dataset(path='{path}') for the "
        f"real column names."
    )


def _check_year(endpoint: dict, year: str | None) -> str | None:
    """Reject a year the dataset doesn't cover, before firing.

    The API answers an out-of-range year with a 500, which is indistinguishable
    from a real server fault.
    """
    if not year:
        return None
    if not year.isdigit():
        range_error = _year_range_message(year)
        if range_error:
            return range_error
        return (
            f"'{year}' is not a valid year. Use a four-digit year (the fall of "
            f"the academic year, so 2024-25 is 2024)."
        )
    available = _parse_available_years(endpoint.get("years_available", ""))
    if not available or int(year) in available:
        return None
    span = html.unescape(endpoint.get("years_available") or "")
    return (
        f"Year {year} is not available for this dataset. Available years: {span}. "
        f"(A year is the fall of the academic year, so 2024-25 is 2024.)"
    )


_YEAR_RANGE_RE = re.compile(r"\d{4}\s*[-–]\s*\d{4}")


def _year_range_message(value: str) -> str | None:
    """The correction for a year range used where one year belongs, else None.

    A year range is a natural thing for a caller to try, and the API answers one
    with a 500 or an empty result rather than an error — an empty result being
    indistinguishable from a legitimately empty query — so it has to be caught
    before the request goes out.
    """
    if not _YEAR_RANGE_RE.fullmatch(value.strip()):
        return None
    start, end = re.findall(r"\d{4}", value)
    return (
        f"'{value}' is a year range, but a path takes ONE year. Request each "
        f"year separately — e.g. the path with {start}, then {end} — and "
        f"combine the results yourself."
    )


async def _unknown_path_error(path: str) -> str:
    """Turn an unmatched path into a menu of real ones rather than a dead end."""
    # A year range is the most likely specific mistake, so name it rather than
    # leaving the caller to infer it from a list of templates.
    for segment in _path_segments(path):
        range_error = _year_range_message(segment)
        if range_error:
            return range_error
    # A near-miss on a placeholder's format gets a targeted answer; a list of
    # templates still reading "{grade}" would not tell the caller anything new.
    specific = await _cache.explain_mismatch(path)
    if specific:
        return specific

    suggestions = await _cache.suggest_paths(path)
    listing = "\n".join(f"  {s}" for s in suggestions) or "  (none similar)"
    return (
        f"No dataset matches the path '{path}'.\n\n"
        f"A path must match a dataset template with every {{placeholder}} filled "
        f"in, and with the same number of segments. Closest known templates:\n"
        f"{listing}\n\n"
        f"Use search_datasets to browse, then fill in the template it returns."
    )


def _parse_available_years(years_str: str) -> set[int]:
    """Expand a years_available string ('1980, 1984&ndash;2024') into the set of
    covered years, so a request for a year the dataset doesn't have fails with a
    clear message instead of a raw 500 from the API."""
    if not years_str:
        return set()
    s = years_str.replace("&ndash;", "-").replace("–", "-")
    out: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                out.update(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


def _parse_latest_year(years_str: str) -> int | None:
    """Latest year covered by a years_available string ('1980, 1984-2024' -> 2024)."""
    years = _parse_available_years(years_str)
    return max(years) if years else None


async def _apply_labels(endpoint_id: int, records: list[dict]) -> list[dict]:
    """Replace coded values with human-readable labels in data records.

    Codes are decoded per variable, from that variable's own `format` and the
    /api-values/ rows for it — never from a global table. The meaning of a code
    is field-specific: grade=-1 is Pre-K, while race=-1 is missing/not reported.
    """
    variables = await _cache.get_endpoint_varlist(endpoint_id)

    # Only consider columns actually present in the result. With `fields=` in
    # play a caller may have asked for 5 of 52 columns, and fetching value maps
    # for the other 47 is a round trip each for labels nobody will see.
    present = set(records[0]) if records else set()

    var_formats = {}
    for var in variables:
        name = var.get("variable", "")
        if name not in present:
            continue
        fmt = var.get("format", "")
        if fmt and fmt not in ("None", "numeric", "string", ""):
            var_formats[name] = fmt
    if not var_formats:
        return records

    # Fetched concurrently — these were serial, and a wide endpoint needs ~10 of
    # them, which dominated cold-start latency.
    formats = sorted(set(var_formats.values()))
    value_lists = await asyncio.gather(*(_cache.get_values(f) for f in formats))
    format_maps = {
        fmt: {v.get("code"): strip_code_prefix(v.get("code"), v.get("code_label", ""))
              for v in values}
        # strict=: gather() returns exactly one result per input coroutine.
        for fmt, values in zip(formats, value_lists, strict=True)
    }

    labeled_records = []
    for record in records:
        labeled = dict(record)
        for var_name, fmt in var_formats.items():
            if var_name in labeled and fmt in format_maps:
                label = format_maps[fmt].get(labeled[var_name])
                if label is not None:
                    labeled[var_name] = label
        labeled_records.append(labeled)
    return labeled_records


def main():
    serve(mcp)


if __name__ == "__main__":
    main()
