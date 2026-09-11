import json
from pathlib import Path

import httpx
import pytest
import respx

from edp_mcp.api_client import EdpClient
from edp_mcp.constants import BASE_URL
from edp_mcp.metadata import MetadataCache

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def endpoints_response():
    return load_fixture("endpoints.json")


@pytest.fixture
def varlist_24_response():
    return load_fixture("varlist_24.json")


@pytest.fixture
def varlist_enrollment_response():
    return load_fixture("varlist_enrollment.json")


@pytest.fixture
def values_fips_response():
    return load_fixture("values_fips.json")


@pytest.fixture
def data_dc_schools_response():
    return load_fixture("data_dc_schools_2022.json")


def _make_values_handler(values_fips_response):
    """Return a respx side_effect that serves fips values or empty results."""
    empty = {"count": 0, "next": None, "previous": None, "results": []}
    # resolve_entity decodes school_level so the column reads "High", not "3".
    school_level = {
        "count": 4, "next": None, "previous": None,
        "results": [
            {"code": 1, "code_label": "1 - Primary"},
            {"code": 2, "code_label": "2 - Middle"},
            {"code": 3, "code_label": "3 - High"},
            {"code": 4, "code_label": "4 - Other"},
        ],
    }

    def handler(request):
        fmt = request.url.params.get("format_name", "")
        if fmt == "fips":
            return httpx.Response(200, json=values_fips_response)
        if fmt == "school_level":
            return httpx.Response(200, json=school_level)
        return httpx.Response(200, json=empty)

    return handler


@pytest.fixture
def mock_api(
    endpoints_response, varlist_24_response, varlist_enrollment_response,
    values_fips_response, data_dc_schools_response,
):
    """Set up respx mocks for all metadata endpoints."""
    # Sample summary response
    summary_response = {
        "count": 3,
        "next": None,
        "previous": None,
        "results": [
            {"year": 2022, "fips": 6, "enrollment": 6000000},
            {"year": 2022, "fips": 11, "enrollment": 90000},
            {"year": 2022, "fips": 48, "enrollment": 5500000},
        ],
    }

    # The CCD enrollment family (25-28). Served their own varlist because the
    # directory one below has no numeric measure and no race/sex, so a summary
    # over it could not be valid — and get_summary now checks that its var and
    # by exist before spending a request on them.
    enrollment_endpoints = {"25", "26", "27", "28"}

    def _varlist_handler(request):
        endpoint_id = request.url.params.get("endpoint_id", "")
        if endpoint_id in enrollment_endpoints:
            return httpx.Response(200, json=varlist_enrollment_response)
        # The sample directory varlist otherwise (it carries fips, so it
        # exercises metadata-driven labeling for both get_data and get_summary).
        return httpx.Response(200, json=varlist_24_response)

    # Provenance-footer metadata. api-changes is deliberately NOT newest-first so
    # the version logic must pick by max release_date (current = 0.25.0).
    sources_response = {
        "count": 1, "next": None, "previous": None,
        "results": [
            {"data_source_id": 2, "data_source": "ccd",
             "label": "Common Core of Data", "link": "https://nces.ed.gov/ccd/"},
        ],
    }
    changes_response = {
        "count": 2, "next": None, "previous": None,
        "results": [
            {"version": "0.1.0 - Beta", "release_date": "4/19/2018"},
            {"version": "0.25.0", "release_date": "3/30/2026"},
        ],
    }
    downloads_response = {
        "count": 2, "next": None, "previous": None,
        "results": [
            {"file_dir": "ccd", "file_name": "codebook_schools_ccd_directory.xls",
             "file_label": "Codebook", "file_size": "46.5 KB"},
            {"file_dir": "ccd", "file_name": "schools_ccd_directory.csv",
             "file_label": "Schools CCD Directory, 1986&ndash;2024", "file_size": "1 GB"},
        ],
    }

    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as mock:
        mock.get("api-endpoints/").respond(json=endpoints_response)
        mock.get("api-endpoint-varlist/").mock(side_effect=_varlist_handler)
        mock.get("api-values/").mock(
            side_effect=_make_values_handler(values_fips_response)
        )
        mock.get("api-sources/").respond(json=sources_response)
        mock.get("api-changes/").respond(json=changes_response)
        mock.get("api-downloads/").respond(json=downloads_response)
        mock.get("schools/ccd/directory/2022/", params={"fips": "11"}).respond(
            json=data_dc_schools_response
        )
        # For resolve_entity with fips filter
        mock.get("schools/ccd/directory/2024/", params={"fips": "11"}).respond(
            json=data_dc_schools_response
        )
        # Small per-year payloads (fips=10) for the multi-year regression test:
        # each year must appear in the result, not just the first.
        # 2018 serves the full DC directory so the multi-year projection is
        # over budget; 2019+ are deliberately NOT mocked, so the test fails loudly
        # if the probe ever fetches years it should have skipped.
        mock.get("schools/ccd/directory/2018/", params={"fips": "11"}).respond(
            json=data_dc_schools_response
        )
        for yr in (2020, 2021, 2022):
            mock.get(f"schools/ccd/directory/{yr}/", params={"fips": "10"}).respond(
                json=_small_year_response(yr)
            )
        # A genuine upstream failure, for the error-handling tests. An unmocked
        # route would instead raise respx's own assertion error, which is not an
        # httpx error and so is not caught by the handlers under test.
        mock.get("schools/ccd/directory/2022/", params={"fips": "99"}).respond(500)
        # For get_summary
        mock.get("schools/ccd/enrollment/summaries").respond(json=summary_response)
        # The aggregation backend's own diagnosis of a bad argument, in the shape
        # it really sends it — the server is expected to relay this verbatim
        # rather than substitute a guess.
        mock.get("schools/meps/summaries").respond(
            400, json=[{"API ERROR": "'nonsense' is not a valid 'var' argument"}]
        )
        # resolve_entity(college) hits the IPEDS directory; served as a genuine
        # upstream failure so the error path is exercised by an httpx error
        # rather than by respx's own (deliberately uncaught) assertion.
        mock.get("college-university/ipeds/directory/2024/").respond(503)
        yield mock


def _small_year_response(year: int, n: int = 2) -> dict:
    """A tiny directory payload tagged with its year."""
    return {
        "count": n,
        "next": None,
        "previous": None,
        "results": [
            {
                "ncessch": f"{year}{i:03d}",
                "year": year,
                "fips": 10,
                "school_name": f"School {i}",
            }
            for i in range(n)
        ],
    }


@pytest.fixture
def client():
    """Create a fresh EdpClient (no async cleanup needed — httpx handles it)."""
    return EdpClient()


@pytest.fixture
def cache(client):
    return MetadataCache(client)


@pytest.fixture(autouse=True)
def _reset_server_cache():
    """Reset the server module's global cache between tests."""
    from edp_mcp import server
    server._client = EdpClient()
    server._cache = MetadataCache(server._client)
