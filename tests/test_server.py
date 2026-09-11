"""Tests for MCP server tools (search_datasets, describe_dataset, get_data,
lookup_codes, resolve_entity, get_summary)."""

import csv
import io

import httpx

from edp_mcp.constants import MAX_RESPONSE_CHARS
from edp_mcp.formatting import format_summary, records_to_csv
from edp_mcp.server import (
    PREVIEW_ROWS,
    describe_dataset,
    get_data,
    get_summary,
    lookup_codes,
    resolve_entity,
    search_datasets,
)


def _csv_rows(result: str) -> list[list[str]]:
    """Parse the CSV block out of a get_data or preview response.

    Located structurally — the block sits between the first blank line and the
    next one — so this works whether or not the caller projected columns, and
    whatever the header happens to be. Parsed with the csv module because school
    names contain commas and are therefore quoted.
    """
    lines = result.splitlines()
    start = lines.index("") + 1
    end = next(
        (i for i in range(start, len(lines)) if not lines[i].strip()), len(lines)
    )
    return list(csv.reader(io.StringIO("\n".join(lines[start:end]))))


def test_format_summary_emits_csv_and_keeps_full_precision():
    """Aggregates are emitted with the API's own column names, and unrounded.

    Size-conditional formatting (`,.0f` above 1000) would drop the decimals from
    large averages while leaving smaller ones at full precision, giving one
    column two different precisions."""
    rows = [{"year": 2022, "fips": "California", "enrollment": 1234.56}]
    out = format_summary(rows, "enrollment", "avg", "fips")
    assert "year,fips,enrollment" in out
    assert "1234.56" in out
    assert "1,235" not in out
    assert "complete result set" in out


async def test_search_datasets(mock_api):
    """Test that search_datasets returns filtered endpoints."""
    result = await search_datasets(level="schools")
    assert "dataset(s)" in result
    assert "schools" in result.lower()


async def test_search_datasets_with_search(mock_api):
    """Test keyword search."""
    result = await search_datasets(search="enrollment")
    assert "enrollment" in result.lower()


async def test_describe_dataset_by_semantic_params(mock_api):
    """Test describe_dataset with level/source/topic instead of endpoint_id."""
    result = await describe_dataset(path="schools/ccd/directory/{year}/")
    assert "variable(s)" in result
    assert "fips" in result.lower()


async def test_describe_dataset_rejects_an_unknown_path(mock_api):
    """Path is the only identifier now; an unknown one becomes a menu."""
    result = await describe_dataset(path="schools/ccd/not-a-dataset/{year}/")
    assert "No dataset matches the path" in result


async def test_describe_dataset_not_found(mock_api):
    """Test describe_dataset with semantic params that don't match."""
    result = await describe_dataset(path="schools/ccd/nonexistent/{year}/")
    assert "No dataset matches the path" in result
    # The dead end becomes a menu of real templates.
    assert "schools/ccd/" in result


async def test_lookup_codes(mock_api):
    """Test lookup_codes returns code-to-label mappings."""
    result = await lookup_codes(format_name="fips")
    assert "fips" in result.lower()
    assert "California" in result


async def test_lookup_codes_specific(mock_api):
    """Test lookup_codes with specific codes."""
    result = await lookup_codes(format_name="fips", codes="6,11")
    assert "California" in result
    assert "District of Columbia" in result


async def test_lookup_codes_no_redundant_code_prefix(mock_api):
    """EDP's code_label is already prefixed with the code ('6 - California'), so
    the rendered line must read '6 = California', not '6 = 6 - California'."""
    result = await lookup_codes(format_name="fips", codes="6")
    assert "6 = California" in result
    assert "6 - California" not in result


async def test_get_data_basic(mock_api):
    """Test get_data returns records."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,school_name,fips", add_labels=False,
    )
    assert "record(s)" in result


async def test_get_data_not_found(mock_api):
    """Test get_data with non-existent endpoint."""
    result = await get_data(path="schools/ccd/nonexistent/2022/")
    assert "No dataset matches the path" in result
    assert "search_datasets" in result


async def test_get_data_add_labels(mock_api):
    """Test that add_labels=True replaces coded values with labels."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,school_name,fips", add_labels=True,
    )
    assert "record(s)" in result
    # With labels, fips=11 should become "District of Columbia"
    assert "District of Columbia" in result


async def test_get_data_no_labels(mock_api):
    """Test that add_labels=False preserves raw coded values."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,school_name,fips", add_labels=False,
    )
    assert "record(s)" in result
    # Without labels the fips COLUMN stays the numeric code. Checked on the
    # parsed column rather than the raw text: "District of Columbia
    # International School" is a school name and appears either way, and school
    # names contain commas, so the CSV has to be parsed rather than split.
    rows = _csv_rows(result)
    assert rows[0] == ["ncessch", "school_name", "fips"]
    assert {r[2] for r in rows[1:]} == {"11"}


async def test_resolve_entity_school(mock_api):
    """Test resolve_entity finds schools by name."""
    result = await resolve_entity(name="Friendship", entity_type="school", fips=11)
    assert "matching school(s)" in result
    assert "Friendship" in result
    # Candidates render as CSV, the same shape as every other table.
    assert "ncessch,school_name,city_location" in result


async def test_resolve_entity_returns_decoded_school_level(mock_api):
    """Level words are matched as noise, so the level has to come back as a
    COLUMN — otherwise "Lincoln Elementary" and "Lincoln High" in one city are
    indistinguishable and the instruction to disambiguate is unfollowable.
    Decoded, because "3" is not something a caller can disambiguate on.
    """
    result = await resolve_entity(name="Friendship", entity_type="school", fips=11)
    assert result.splitlines()[2].endswith(",school_level")
    # Decoded, not the raw code: no data row ends in a bare integer.
    assert "High" in result and "Primary" in result
    assert not any(line.rstrip().endswith(",3") for line in result.splitlines())


async def test_resolve_entity_only_fetches_a_level_column_for_schools(mock_api):
    """Districts and colleges span grade levels, so there is no equivalent
    column to ask for. Asserted on the outgoing REQUEST rather than the rendered
    output: the mock has no college directory route, so checking the response
    text would pass for the wrong reason.
    """
    await resolve_entity(name="Harvard", entity_type="college")
    fetched = [
        str(c.request.url) for c in mock_api.calls
        if "ipeds/directory" in str(c.request.url)
    ]
    assert fetched, "expected a college directory fetch to be attempted"
    assert "school_level" not in fetched[0]


async def test_resolve_entity_invalid_type(mock_api):
    """Test resolve_entity rejects invalid entity types."""
    result = await resolve_entity(name="Test", entity_type="invalid")
    assert "Invalid entity_type" in result


def _match(query, candidate):
    from edp_mcp.server import _entity_matches
    return _entity_matches(query, candidate)


def test_entity_match_bridges_ccd_abbreviations():
    """Natural phrasings match CCD's abbreviated directory names."""
    assert _match("Lakewood Elementary", "LAKEWOOD EL")
    assert _match("Lamar High School", "LAMAR H S")
    assert _match("Jefferson Middle School", "JEFFERSON MS")
    assert _match("Lamar Intermediate", "LAMAR INT")
    assert _match("Lamar Primary", "LAMAR PRI")
    # District vocabulary: full-name query finds the abbreviated LEA name.
    assert _match("Crosbyton Independent School District", "CROSBYTON CISD")
    # Substring and bare distinctive token still match.
    assert _match("Friendship", "Friendship PCS - Collegiate")
    assert _match("Lamar", "LAMAR EL")


def test_entity_match_requires_the_distinctive_words():
    """Type words carry no signal; the distinctive ones must all be present."""
    assert not _match("Lincoln Elementary", "WASHINGTON H S")
    # A query of only structural words is too generic to match anything.
    assert not _match("High School", "LAMAR H S")


def test_entity_match_does_not_rank_by_grade_level():
    """Level words are noise, not a ranking signal.

    Scoring candidates by grade level takes a pile of heuristics and can still
    put the wrong school first. Both of these simply match, and the caller
    disambiguates on city — which is what the tool tells it to do.
    """
    assert _match("Lakewood Elementary", "LAKEWOOD HS")
    assert _match("Lakewood High School", "LAKEWOOD EL")


async def test_resolve_entity_no_matches(mock_api):
    """Test resolve_entity returns helpful message when no matches."""
    result = await resolve_entity(name="ZZZZNONEXISTENT", entity_type="school", fips=11)
    assert "No school" in result or "0 matching" in result.lower()


async def test_get_summary_basic(mock_api):
    """Test get_summary returns aggregated results."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum", by="fips",
    )
    assert "Summary:" in result
    assert "SUM(enrollment)" in result
    assert "fips" in result
    # Grouping codes should be decoded from API metadata (fips=6 → California)
    assert "California" in result


async def test_get_summary_with_filter(mock_api):
    """Test get_summary with filter."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum", by="race",
        filters="fips=6",
    )
    assert "Summary:" in result
    assert "filtered by" in result


async def test_get_summary_invalid_stat(mock_api):
    """Test get_summary rejects invalid statistics."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="invalid", by="fips",
    )
    assert "Invalid stat" in result


# --- Provenance footer ------------------------------------------------------

async def test_get_data_footer_present(mock_api):
    """get_data appends a provenance footer with verified, pulled fields."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,school_name,fips", add_labels=False,
    )
    # Two-layer source (pulled label + code), via the EDP
    assert "Source: Common Core of Data (CCD) — via Urban Institute Education Data Portal" in result
    # License (verified constant)
    assert "License: Open Data Commons Attribution License (ODC-By v1.0)" in result
    # The exact, directly-working request URL is echoed
    expected_url = (
        "API URL: https://educationdata.urban.org/api/v1"
        "/schools/ccd/directory/2022/?fips=11"
    )
    assert expected_url in result
    # Version picked by max release_date, not list order (results aren't newest-first)
    assert "Education Data Portal v. 0.25.0" in result
    assert "0.1.0" not in result
    assert "Common Core of Data (2022)" not in result


async def test_get_summary_footer_present(mock_api):
    """get_summary footer points at the summaries URL with a trailing slash."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum", by="fips",
    )
    assert "Source: Common Core of Data (CCD) — via Urban Institute" in result
    assert "/enrollment/summaries/?var=enrollment" in result  # trailing slash, no 301
    assert "Education Data Portal v. 0.25.0" in result


async def test_get_data_no_invented_agency(mock_api):
    """Regression: never print an agency like 'NCES' — it's not an API field."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,school_name,fips", add_labels=False,
    )
    assert "NCES" not in result


# --- Bulk-download escape hatch (#3) ----------------------------------------

async def test_get_data_download_hatch_on_refusal(mock_api):
    """A refused (over-budget) result offers the complete CSV from api-downloads."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11", add_labels=False,
    )
    assert "RESULT TOO LARGE" in result
    # URL built only from real api-downloads fields, and the codebook .xls is excluded
    assert "https://educationdata.urban.org/csv/ccd/schools_ccd_directory.csv" in result
    assert "codebook" not in result.lower()


# --- Complete-or-refuse -----------------------------------------------------

async def test_over_budget_query_returns_no_rows_at_all(mock_api):
    """The core guarantee: an over-large result yields NOTHING, not a slice.

    A truncated slice is ordered by ID, so it is not a valid sample for counting,
    ranking or averaging — but it reads exactly like a complete answer. The whole
    point is that no data escapes when the full set can't be returned.
    """
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11", add_labels=False,
    )
    assert "RESULT TOO LARGE" in result
    # The true size is reported, and it is actionable.
    assert "244 rows" in result
    assert "fields=" in result
    assert "get_summary" in result
    # No record data leaked into the refusal.
    assert "Friendship" not in result


async def test_projection_brings_a_refused_query_under_budget(mock_api):
    """fields= is the documented way out of a refusal, and it works."""
    refused = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11", add_labels=False,
    )
    allowed = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11", add_labels=False,
        fields="ncessch,school_name,fips",
    )
    assert "RESULT TOO LARGE" in refused
    assert "RESULT TOO LARGE" not in allowed
    assert "complete result set" in allowed
    # Projected to exactly the requested columns.
    assert allowed.splitlines()[2] == "ncessch,school_name,fips"


async def test_preview_returns_a_sample_that_cannot_pass_as_a_result(mock_api):
    """preview=True is the only sanctioned partial output, so the labelling is
    what keeps it safe: it must say it is partial, say how many rows exist, and
    say not to compute over it — before AND after the rows, since a long CSV
    block pushes a single header out of sight."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11", add_labels=False,
    )
    preview = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11", add_labels=False, preview=True,
    )
    # Without preview this query is refused outright.
    assert "RESULT TOO LARGE" in result
    # With it, rows come back — labelled, capped, and honest about the total.
    assert "PREVIEW — NOT A RESULT SET" in preview
    assert "of 244 matching rows" in preview
    assert "Do NOT count, rank, average" in preview
    assert preview.count("Do NOT count, rank, average") == 2  # repeated after the rows
    assert "not a random sample" in preview
    # Capped at PREVIEW_ROWS, and never claims completeness.
    assert len(_csv_rows(preview)) - 1 <= PREVIEW_ROWS
    assert "complete result set" not in preview


async def test_preview_narrows_further_on_very_wide_tables(mock_api):
    """25 rows of a 96-column table would still blow the budget, so a preview
    shows the row limit or whatever actually fits, whichever is smaller."""
    from edp_mcp.server import _rows_that_fit
    narrow = [{"a": 1, "b": 2}] * 100
    wide = [{f"col_{i}": "x" * 200 for i in range(96)}] * 100
    assert len(_rows_that_fit(narrow, None, PREVIEW_ROWS)) == PREVIEW_ROWS
    assert 1 <= len(_rows_that_fit(wide, None, PREVIEW_ROWS)) < PREVIEW_ROWS
    # Whatever it shows must genuinely fit — that is the point of measuring.
    shown = _rows_that_fit(wide, None, PREVIEW_ROWS)
    assert len(records_to_csv(shown, None)) <= MAX_RESPONSE_CHARS
    assert _rows_that_fit([], None, PREVIEW_ROWS) == []


async def test_year_range_in_path_is_rejected_with_instructions(mock_api):
    """A year range is a natural thing for a caller to try.

    The API answers a range segment with a 500 or an empty result rather than an
    error, so it has to be caught here — an empty result is indistinguishable
    from a legitimately empty query.
    """
    result = await get_data(path="schools/ccd/directory/2018-2022/")
    assert "is a year range" in result
    assert "ONE year" in result
    assert "2018" in result and "2022" in result
    # Nothing was requested upstream.
    assert not any("directory/2018" in str(c.request.url) for c in mock_api.calls)


async def test_each_year_is_a_separate_complete_request(mock_api):
    """One year per call, each complete.

    A multi-year request sharing one row budget across the range returns only
    the first year while reporting itself complete, so each year is its own
    request with its own budget.
    """
    for year in (2020, 2021, 2022):
        result = await get_data(path=f"schools/ccd/directory/{year}/", filters="fips=10")
        assert "RESULT TOO LARGE" not in result
        assert "Returned 2 record(s)" in result
        assert f",{year}," in result


# --- Structured, recovery-oriented errors (#1) ------------------------------

async def test_get_summary_error_is_actionable(mock_api):
    """A failed summary relays the portal's own diagnosis, not a guess.

    The backend names the single wrong argument. Substituting a message that
    hedges across the endpoint, the var and the by sends the caller to re-check
    three things when only one is wrong.
    """
    # var and by are both valid locally, so the request goes out and the mocked
    # 400 — carrying the backend's own wording — is what has to surface.
    result = await get_summary(
        path="schools/meps", var="enrollment", stat="sum", by="fips",
    )
    assert "'nonsense' is not a valid 'var' argument" in result
    assert "does not support summaries" not in result


async def test_get_summary_rejects_data_path_with_the_summary_path(mock_api):
    """A data path used as a summary path is answered with the right path.

    The portal returns a bare 500 for these, so the correction has to come from
    here — and it is a lookup, not a guess: the path names a real dataset.
    """
    result = await get_summary(
        path="schools/ccd/enrollment/{year}/{grade}/race", var="enrollment",
        stat="sum", by="fips",
    )
    assert "schools/ccd/enrollment" in result
    assert "data path" in result


async def test_get_summary_uses_derived_path_not_template_head(mock_api):
    """Endpoints whose summary lives under a subtopic resolve to the real path.

    The static head of these templates is not a routable summary endpoint;
    requesting it returns a 500 that explains nothing.
    """
    result = await get_summary(
        path="college-university/ipeds/fall-enrollment", var="enrollment_fall",
        stat="sum", by="fips",
    )
    assert "college-university/ipeds/fall-enrollment/race" in result


async def test_get_summary_allows_directory_joined_fields(mock_api):
    """Groupings and filters from the source's directory are accepted.

    Summary tables are pre-joined against the directory, so school_level is a
    valid grouping on enrollment even though it is not an enrollment variable.
    Rejecting it refuses a query the portal would have answered.
    """
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum",
        by="school_level", filters="charter=1",
    )
    assert "Cannot filter on" not in result
    assert "not a valid" not in result


async def test_get_summary_flags_single_year_filter(mock_api):
    """A one-year summary says the whole series costs the same single call."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum", by="fips",
        filters="year=2022",
    )
    assert "every year in one call" in result.lower()
    assert "1986" in result  # the coverage actually available


async def test_get_data_error_is_actionable(mock_api):
    """An upstream fetch failure returns guidance, not a raw exception."""
    # fips=99 isn't mocked → the fetch raises → caught and turned into a message.
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=99", add_labels=False,
    )
    assert "Could not fetch" in result
    assert "describe_dataset" in result


async def test_resolve_entity_error_is_actionable(mock_api):
    """A failed entity search steers the caller toward fips instead of raising."""
    result = await resolve_entity(name="Lincoln", entity_type="college")
    assert "Could not search" in result
    assert "fips" in result


async def test_get_data_unavailable_year(mock_api):
    """A well-formed but uncovered year (endpoint 24 covers 1986–2024) is caught
    before firing, with the available span — not a raw 500 from the API."""
    result = await get_data(path="schools/ccd/directory/2050/")
    assert "not available" in result
    assert "1986–2024" in result
    # It never fired the request.
    assert not any("directory/2050" in str(c.request.url) for c in mock_api.calls)


async def test_get_data_available_year_passes(mock_api):
    """A covered year proceeds normally."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,school_name,fips", add_labels=False,
    )
    assert "not available" not in result
    assert "record(s)" in result


# --- describe_dataset header (endpoint + source + sample code) --------------

async def test_describe_dataset_shows_header(mock_api):
    """describe_dataset now prepends the endpoint's own description and sample
    code above the variable list, so 'which dataset' steering reaches the model."""
    result = await describe_dataset(path="schools/ccd/directory/{year}/")
    assert "Dataset: schools/ccd/directory/{year}" in result
    assert "Description:" in result
    assert "Sample code" in result
    assert "get_education_data" in result  # unescaped R snippet
    # The variable list still follows the header.
    assert "variable(s)" in result


# --- get_data pre-flight validation -----------------------------------------

async def test_path_missing_a_required_segment_names_the_placeholder(mock_api):
    """A path short a segment fails cleanly AND says which placeholder is wrong.

    Handing back a template that still reads "{grade}" tells the caller nothing
    it did not already have, so a near-miss on a placeholder's format gets a
    targeted answer instead. Passing "99" where "grade-99" is required is the
    likeliest way a path comes out wrong.
    """
    result = await get_data(path="schools/ccd/enrollment/2020/race/")  # no grade
    assert "not a valid {grade} value" in result
    assert "grade-99" in result  # the required segment form
    assert not any("enrollment/2020" in str(c.request.url) for c in mock_api.calls)


async def test_bare_grade_number_is_corrected_not_just_rejected(mock_api):
    """The exact mistake a caller makes when reading the variable rather than
    the path: grade 99 is the segment "grade-99"."""
    result = await get_data(path="schools/ccd/enrollment/2022/99/race/")
    assert "'99' is not a valid {grade} value" in result
    assert "grade-99" in result
    assert not any("enrollment/2022" in str(c.request.url) for c in mock_api.calls)


async def test_get_data_unknown_filter_key(mock_api):
    """A filter key that isn't a real variable is rejected, rather than being
    silently ignored by the API and returning unfiltered data."""
    result = await get_data(path="schools/ccd/directory/2022/", filters="state=6")
    assert "Cannot filter on: state" in result
    assert "Valid filter fields" in result
    # No directory data request went out.
    assert not any("directory/2022" in str(c.request.url) for c in mock_api.calls)


async def test_get_data_rejects_filter_on_a_non_filterable_column(mock_api):
    """`enrollment` is a real variable but is_filter=0. The API answers
    `?enrollment=500` with every row, HTTP 200, no warning — so accepting any
    known variable name waved through exactly the silently-unfiltered result
    this validation exists to prevent."""
    result = await get_data(path="schools/ccd/directory/2022/", filters="enrollment=500")
    assert "Cannot filter on: enrollment" in result
    assert "is a variable in this dataset but not filterable" in result
    assert "UNFILTERED" in result
    # It never fired the request that would have returned everything.
    assert not any("directory/2022" in str(c.request.url) for c in mock_api.calls)


async def test_ranked_query_answers_where_an_unranked_one_refuses(mock_api):
    """`ordering=` is the escape hatch complete-or-refuse needs.

    The API sorts the whole result before it pages, so the rows that come back
    are the true top of the set, not the arbitrary ID-ordered prefix the refusal
    rule exists to suppress. Refusing it would leave "which are the biggest N"
    unanswerable on any table too large to return whole.
    """
    refused = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11", add_labels=False,
    )
    ranked = await get_data(
        path="schools/ccd/directory/2022/",
        filters="fips=11&ordering=-enrollment", add_labels=False,
    )
    assert "RESULT TOO LARGE" in refused
    assert "RESULT TOO LARGE" not in ranked
    # Says what it is: a real top-N, sized and attributed to the API's sort.
    assert "ranked by the API" in ranked
    assert "of 244 matching rows" in ranked
    assert "enrollment (highest first)" in ranked
    # And what it is not — a subset is valid for ranking, not for totals.
    assert "do not total, average, or count over them" in ranked.lower()
    # Cited like any other answer, with ordering= in the echoed URL so the
    # ranking is reproducible rather than taken on trust.
    assert "Cite:" in ranked
    assert "ordering=-enrollment" in ranked


async def test_ranked_ascending_is_described_as_lowest_first(mock_api):
    """`ordering=enrollment` (no minus) is ascending, so the rows are the LOWEST.
    Mislabelling the direction would invert the answer silently."""
    ranked = await get_data(
        path="schools/ccd/directory/2022/",
        filters="fips=11&ordering=enrollment", add_labels=False,
    )
    assert "enrollment (lowest first)" in ranked


async def test_unknown_ordering_column_is_rejected_before_it_can_fake_a_ranking(
    mock_api,
):
    """The API answers ordering=<nonsense> with 200 and rows in ID order —
    verified live 2026-07-28, byte-identical to sending no ordering at all.

    Unchecked, a typo turns an arbitrary ID-ordered prefix into rows presented as
    "genuinely the highest". That is the one output this server must never
    produce, and it is the ranked path — the path allowed to skip
    complete-or-refuse precisely because its rows are meaningful.
    """
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11&ordering=-enrolment",
    )
    assert "Unknown column(s)" in result
    assert "ordering=enrolment" in result
    # Refused before firing, so no fake ranking could come back.
    assert not any("directory/2022" in str(c.request.url) for c in mock_api.calls)


async def test_unknown_fields_column_is_rejected(mock_api):
    """An unknown fields= column comes back as a silent blank column, not an
    error, so the caller reads absent data as missing data."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,enrolment",
    )
    assert "Unknown column(s)" in result
    assert "fields=enrolment" in result


def test_ascending_rank_over_sentinel_codes_is_not_called_meaningful():
    """-1/-2/-3 are codes, not quantities, and they sort below every real value.

    So "the 10 smallest schools by enrollment" returns suppressed and missing
    rows. Answering that with "these are genuinely the lowest" is a confident
    wrong answer; the ranked header has to say the ranking is not usable.
    """
    from edp_mcp.formatting import format_ranked
    suppressed = [{"ncessch": "1", "enrollment": -3}, {"ncessch": "2", "enrollment": -1}]
    real = [{"ncessch": "1", "enrollment": 12}, {"ncessch": "2", "enrollment": 40}]

    bad = format_ranked(suppressed, 500, "enrollment")
    assert "RANKING NOT MEANINGFUL" in bad
    assert "genuinely the lowest" not in bad

    # A clean ascending rank keeps the plain claim.
    assert "genuinely the lowest" in format_ranked(real, 500, "enrollment")
    # Descending is unaffected: sentinels sort to the bottom, out of the way.
    assert "genuinely the highest" in format_ranked(real, 500, "-enrollment")


async def test_summary_rejects_ordering_with_the_reason(mock_api):
    """get_data allows ordering=, so get_summary must refuse it explicitly — the
    API 400s on ordering= for /summaries/, and a bare 400 reads like a schema
    error the caller would go re-check describe_dataset for."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum", by="fips",
        filters="ordering=-enrollment",
    )
    assert "cannot rank aggregates" in result
    assert "get_data" in result


async def test_narrowing_hint_never_suggests_a_coarser_filter(mock_api):
    """The hint must be finer than what the query already uses.

    Telling a caller who already filtered to one district to "add fips" is not
    narrowing — it would not drop a single row — and bottoming out at "restrict
    to one school" answers a different question than the one asked.
    """
    from edp_mcp.server import _narrowing_hint
    variables = [
        {"variable": v, "is_filter": "1"}
        for v in ("fips", "leaid", "ncessch", "enrollment")
    ]
    # Nothing scoped yet: start at the coarsest.
    assert "add fips=" in _narrowing_hint({}, variables)
    # Scoped to a state: the next step down is a district.
    assert "add leaid=" in _narrowing_hint({"fips": "6"}, variables)
    # Already at one district — the only finer key is a single school, so the
    # hint falls through instead of suggesting either.
    hint = _narrowing_hint({"leaid": "0622710"}, variables)
    assert "fips" not in hint and "ncessch" not in hint


async def test_ordering_is_accepted_as_a_query_param(mock_api):
    """`ordering` is a real API param, not a variable, so filter validation must
    let it through the same way it lets `fields` through."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11&ordering=-enrollment",
        fields="ncessch,school_name,enrollment", add_labels=False,
    )
    assert "Cannot filter on" not in result
    assert any("ordering=-enrollment" in str(c.request.url) for c in mock_api.calls)


async def test_get_data_valid_filter_passes_validation(mock_api):
    """A known filter key (fips) sails through validation and returns records."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,school_name,fips", add_labels=False,
    )
    assert "record(s)" in result
    assert "Unknown filter" not in result


async def test_malformed_filter_pair_is_rejected_not_dropped(mock_api):
    """A pair that isn't key=value is rejected, never skipped.

    Skipping it sends the request WITHOUT that filter, so "fips:11" returns every
    school in the country under a header the caller reads as filtered — the exact
    silent wrongness that unsupported filter keys are rejected to prevent.
    """
    result = await get_data(path="schools/ccd/directory/2022/", filters="fips:11")
    assert "Invalid filter" in result
    assert "fips:11" in result
    # Nothing was requested, so no unfiltered data could come back.
    assert not any("directory/2022" in str(c.request.url) for c in mock_api.calls)


# --- get_summary validates like get_data ------------------------------------

async def test_get_summary_rejects_an_unsupported_filter(mock_api):
    """A summary silently losing its filter is worse than a data query doing so:
    "enrollment by state" filtered to nothing still looks like a valid table."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum", by="fips",
        filters="charterr=1",
    )
    assert "Cannot filter on: charterr" in result
    # Refused before firing, so no unfiltered aggregate was ever computed.
    assert not any("summaries" in str(c.request.url) for c in mock_api.calls)


async def test_get_summary_accepts_a_filter_valid_on_a_sibling_template(mock_api):
    """Validation spans every template sharing the summary path, so a field that
    only exists on the by-race variant is not rejected on the plain one."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum", by="fips",
        filters="fips=6",
    )
    assert "Cannot filter on" not in result
    assert "Summary:" in result


async def test_get_summary_rejects_a_year_the_data_does_not_cover(mock_api):
    """CCD enrollment starts in 1986; asking for 1850 got a raw upstream 500."""
    result = await get_summary(
        path="schools/ccd/enrollment", var="enrollment", stat="sum", by="fips",
        filters="year=1850",
    )
    assert "1850" in result and "not available" in result
    assert not any("summaries" in str(c.request.url) for c in mock_api.calls)


# --- resolve_entity directory scan is cached --------------------------------

async def test_directory_scan_is_fetched_once_per_state(mock_api):
    """The scan is the most expensive fetch the server makes — a state's school
    directory is megabytes — and resolving several names in one state is the
    normal case, so it must not be re-downloaded per lookup."""
    await resolve_entity(name="Lakewood", entity_type="school", fips=11)
    await resolve_entity(name="Friendship", entity_type="school", fips=11)
    scans = [c for c in mock_api.calls if "directory/2024" in str(c.request.url)]
    assert len(scans) == 1


# --- Traffic tagging (mode=mcp) ---------------------------------------------

async def test_requests_carry_mode_mcp(mock_api):
    """Every portal request is tagged mode=mcp for traffic tracking, but the
    tag never leaks into the cited provenance URL."""
    result = await get_data(
        path="schools/ccd/directory/2022/", filters="fips=11",
        fields="ncessch,school_name,fips", add_labels=False,
    )
    # On the wire: every outgoing request carries the tag.
    assert mock_api.calls  # sanity: requests were actually made
    assert all("mode=mcp" in str(c.request.url) for c in mock_api.calls)
    # In the citation: the echoed API URL stays clean.
    assert (
        "API URL: https://educationdata.urban.org/api/v1/schools/ccd/directory/2022/?fips=11"
        in result
    )
    assert "mode=mcp" not in result


async def test_resolve_entity_asks_for_state_instead_of_guessing(mock_api):
    """No state, no guess: the tool asks rather than answering from one state.

    This is the original failure turned inside out. A bare "Homestead High" used
    to come back as the California school with no sign that other states existed;
    now it comes back as a question, which the agent puts to the user.
    """
    result = await resolve_entity(name="Homestead High", entity_type="school")

    assert "Which state?" in result
    assert "ASK THE USER" in result
    assert "ncessch=" not in result  # no candidate is offered, so none can be wrong


async def test_resolve_entity_state_prompt_costs_nothing(mock_api):
    """The prompt short-circuits before any directory request is made."""
    calls = {"n": 0}

    def counting_handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"count": 0, "next": None, "previous": None, "results": []})

    mock_api.get("schools/ccd/directory/2024/").mock(side_effect=counting_handler)
    await resolve_entity(name="Homestead High", entity_type="school")
    assert calls["n"] == 0


async def test_resolve_entity_college_does_not_ask(mock_api):
    """Colleges are one small page — no state needed, so no question asked."""
    result = await resolve_entity(name="Harvard", entity_type="college")
    assert "Which state?" not in result


def test_instructions_survive_client_truncation():
    """Server instructions must fit inside the smallest client budget.

    MCP clients truncate this field silently — the Claude Code CLI cuts at 2,048
    characters mid-sentence and reports nothing, and the Messages API connector
    appears to drop the field entirely. This block once ran to 2,698, so COST
    and NOT COVERED never reached a model through the CLI, and a citation
    directive added at offset 2,064 did nothing at all.

    Guidance that must reach the model belongs in a tool description or the
    result footer. This test guards the budget, not the wording.
    """
    from edp_mcp.server import mcp

    assert len(mcp.instructions) < 2048
