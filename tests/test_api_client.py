import httpx
import respx

from edp_mcp.api_client import EdpClient
from edp_mcp.constants import BASE_URL


async def test_get_endpoints(mock_api, client, endpoints_response):
    """Test that get_endpoints returns all endpoints."""
    endpoints = await client.get_endpoints()
    assert len(endpoints) == len(endpoints_response["results"])
    assert endpoints[0]["endpoint_id"] is not None



async def test_get_endpoint_varlist(mock_api, client, varlist_24_response):
    """Test that get_endpoint_varlist returns variables for an endpoint."""
    varlist = await client.get_endpoint_varlist(24)
    assert len(varlist) == len(varlist_24_response["results"])
    # Should have common fields
    first_var = varlist[0]
    assert "variable" in first_var
    assert "label" in first_var



async def test_get_values(mock_api, client, values_fips_response):
    """Test that get_values returns code-to-label mappings."""
    values = await client.get_values("fips")
    assert len(values) == len(values_fips_response["results"])
    # Check a known FIPS code
    fips_6 = [v for v in values if v.get("code") == 6]
    assert len(fips_6) == 1
    assert "California" in fips_6[0].get("code_label", "")



async def test_fetch_data(mock_api, client, data_dc_schools_response):
    """Test that fetch_data returns records and count."""
    records, count = await client.fetch_data(
        "schools/ccd/directory/2022/", params={"fips": "11"}
    )
    assert count == data_dc_schools_response["count"]
    assert len(records) <= count



async def test_fetch_data_respects_max_records(mock_api, client):
    """Test that fetch_data caps results at max_records."""
    records, count = await client.fetch_data(
        "schools/ccd/directory/2022/", params={"fips": "11"}, max_records=5
    )
    assert len(records) == 5
    assert count > 5


async def test_429_is_retried(values_fips_response):
    """A 429 is retried (honoring Retry-After), not re-raised like other 4xx."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as mock:
        responses = iter([
            httpx.Response(429, headers={"Retry-After": "0"}, json={}),
            httpx.Response(200, json=values_fips_response),
        ])
        route = mock.get("api-values/").mock(side_effect=lambda req: next(responses))

        client = EdpClient()
        values = await client.get_values("fips")
        # Succeeded on the retry, and both attempts were made.
        assert len(values) == len(values_fips_response["results"])
        assert route.call_count == 2


async def test_404_is_not_retried():
    """A non-429 4xx fails fast — retrying won't fix it."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as mock:
        route = mock.get("api-values/").respond(404, json={})
        client = EdpClient()
        raised = False
        try:
            await client.get_values("fips")
        except httpx.HTTPStatusError:
            raised = True
        assert raised
        assert route.call_count == 1  # no retries


async def test_fetch_all_pages_respects_max_rows():
    """max_rows caps accumulation and stops paging early (backs the summary cap)."""
    page1 = {
        "count": 4, "next": f"{BASE_URL}api-changes/?page=2", "previous": None,
        "results": [{"a": 1}, {"a": 2}],
    }
    page2 = {"count": 4, "next": None, "previous": None, "results": [{"a": 3}, {"a": 4}]}

    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as mock:
        responses = iter([httpx.Response(200, json=page1), httpx.Response(200, json=page2)])
        route = mock.get("api-changes/").mock(side_effect=lambda req: next(responses))

        client = EdpClient()
        rows, total = await client._paginate("api-changes/", max_rows=2)
        assert len(rows) == 2
        assert route.call_count == 1  # page 2 was never fetched


async def test_pagination(endpoints_response):
    """Test that pagination follows next links."""
    page1 = {
        "count": 4,
        "next": f"{BASE_URL}api-endpoints/?page=2",
        "previous": None,
        "results": endpoints_response["results"][:2],
    }
    page2 = {
        "count": 4,
        "next": None,
        "previous": None,
        "results": endpoints_response["results"][2:4],
    }

    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as mock:
        # Use side_effect to return page1 first, then page2
        responses = iter([
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ])
        mock.get("api-endpoints/").mock(side_effect=lambda req: next(responses))

        client = EdpClient()
        endpoints = await client.get_endpoints()
        assert len(endpoints) == 4


async def test_pagination_preserves_next_url_query():
    """Following a `next` link keeps its query string.

    Regression: the client carries a default `mode=mcp` param, and httpx replaces
    (not merges) a URL's query when client params are set — so `?page=2` was
    dropped and every "next page" refetched page 1, duplicating rows up to the
    caller's cap. Assert on the request URLs, not just the row count: mocking by
    path alone cannot tell the two apart.
    """
    page1 = {
        "count": 4, "next": f"{BASE_URL}schools/ccd/directory/2024/?page=2",
        "previous": None, "results": [{"ncessch": "1"}, {"ncessch": "2"}],
    }
    page2 = {
        "count": 4, "next": None, "previous": None,
        "results": [{"ncessch": "3"}, {"ncessch": "4"}],
    }

    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as mock:
        def handler(request):
            page = page2 if request.url.params.get("page") == "2" else page1
            return httpx.Response(200, json=page)

        route = mock.get("schools/ccd/directory/2024/").mock(
            side_effect=handler
        )

        client = EdpClient()
        records, count = await client.fetch_data("schools/ccd/directory/2024/")

        assert route.call_count == 2
        assert route.calls[1].request.url.params.get("page") == "2"  # not dropped
        assert route.calls[1].request.url.params.get("mode") == "mcp"  # still tagged
        assert [r["ncessch"] for r in records] == ["1", "2", "3", "4"]
        assert count == 4


async def test_explicit_params_win_over_next_url_query():
    """A caller's params override same-named ones carried on the URL."""
    path, params = EdpClient._absorb_query("dir/?fips=6&page=2", {"fips": 55})
    assert path == "dir/"
    assert params == {"fips": 55, "page": "2"}
