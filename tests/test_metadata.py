from edp_mcp.constants import DIRECTORY_CACHE_ENTRIES
from edp_mcp.metadata import MetadataCache


class _StubClient:
    """Minimal stand-in for EdpClient: counts fetches, returns one row."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch_data(self, path, params=None, max_records=None):
        self.calls += 1
        return ([{"ncessch": "1", "school_name": "X"}], 1)


async def test_directory_cache_is_bounded():
    """Unlike the metadata caches, entries here are whole state directories
    (California is ~10.5K rows), so an unbounded one would grow without limit on
    a long-running hosted server."""
    cache = MetadataCache(_StubClient())
    for fips in range(DIRECTORY_CACHE_ENTRIES + 3):
        await cache.get_directory("schools/ccd/directory", 2024, ["ncessch"], fips)
    assert len(cache._directory_cache) <= DIRECTORY_CACHE_ENTRIES


async def test_directory_cache_serves_repeat_lookups_from_memory():
    """The scan is the most expensive fetch the server makes, and resolving
    several names in one state is the normal case."""
    client = _StubClient()
    cache = MetadataCache(client)
    for _ in range(3):
        await cache.get_directory("schools/ccd/directory", 2024, ["ncessch"], 11)
    assert client.calls == 1


def test_dedupe_varlist_merges_duplicate_rows():
    """The API returns two rows per variable (one with the description, one a
    bare 'None' stub that carries the values codes). Merge to one full row."""
    rows = [
        {"variable": "allegations_harass_race",
         "label": "Number of allegations ... race, color, or national origin",
         "description": "Harassment on the basis of race refers to ...",
         "values": "None"},
        {"variable": "allegations_harass_race",
         "label": "Number of allegations ... race",
         "description": "None",
         "values": '"-3" : "Suppressed data"'},
    ]
    out = MetadataCache._dedupe_varlist(rows)
    assert len(out) == 1
    merged = out[0]
    # Real description (from row 1) and values codes (from row 2) both survive.
    assert merged["description"].startswith("Harassment on the basis of race")
    assert merged["values"] == '"-3" : "Suppressed data"'


def test_dedupe_varlist_keeps_distinct_variables_and_order():
    rows = [
        {"variable": "b", "description": "None"},
        {"variable": "b", "description": "Def B."},
        {"variable": "a", "description": "Def A."},
    ]
    out = MetadataCache._dedupe_varlist(rows)
    assert [r["variable"] for r in out] == ["b", "a"]  # first-appearance order
    assert out[0]["description"] == "Def B."


async def test_cache_endpoints(mock_api, cache):
    """Test that endpoints are cached after first fetch."""
    endpoints1 = await cache.get_endpoints()
    endpoints2 = await cache.get_endpoints()
    assert endpoints1 is endpoints2  # same object = cached



async def test_cache_varlist(mock_api, cache):
    """Test that variable lists are cached per endpoint_id."""
    varlist1 = await cache.get_endpoint_varlist(24)
    varlist2 = await cache.get_endpoint_varlist(24)
    assert varlist1 is varlist2  # same object = cached



async def test_filter_by_level(mock_api, cache):
    """Test filtering endpoints by level (section)."""
    school_eps = await cache.filter_endpoints(level="schools")
    all_eps = await cache.get_endpoints()

    assert len(school_eps) > 0
    assert len(school_eps) < len(all_eps)
    for ep in school_eps:
        assert ep["section"].lower() == "schools"



async def test_filter_by_source(mock_api, cache):
    """Test filtering endpoints by source."""
    ccd_eps = await cache.filter_endpoints(source="ccd")
    assert len(ccd_eps) > 0
    for ep in ccd_eps:
        assert "ccd" in ep.get("endpoint_url", "").lower()



async def test_filter_by_search(mock_api, cache):
    """Test filtering endpoints by search term."""
    enrollment_eps = await cache.filter_endpoints(search="enrollment")
    assert len(enrollment_eps) > 0
    for ep in enrollment_eps:
        text = (ep.get("endpoint_url", "") + " " + (ep.get("description", "") or "")).lower()
        assert "enrollment" in text



async def test_match_path_resolves_a_filled_in_template(mock_api, cache):
    """A concrete path resolves to its dataset and yields its path params."""
    endpoint, params = await cache.match_path("schools/ccd/directory/2022/")
    assert endpoint["endpoint_id"] == 24
    assert params == {"year": "2022"}


async def test_match_path_tolerates_prefix_and_slashes(mock_api, cache):
    """Callers paste paths in several shapes; all resolve to the same dataset."""
    for variant in (
        "schools/ccd/directory/2022",
        "/schools/ccd/directory/2022/",
        "/api/v1/schools/ccd/directory/2022/",
    ):
        endpoint, _ = await cache.match_path(variant)
        assert endpoint is not None and endpoint["endpoint_id"] == 24, variant


async def test_match_path_requires_an_exact_segment_count(mock_api, cache):
    """A wrong path must fail rather than resolve to a neighbouring dataset.

    A prefix-matching lookup lets an unrecognised segment quietly settle on a
    different endpoint and return the wrong data, so segment counts must match
    exactly.
    """
    endpoint, _ = await cache.match_path("schools/ccd/directory")          # no year
    assert endpoint is None
    endpoint, _ = await cache.match_path("schools/ccd/directory/2022/race/")  # extra
    assert endpoint is None
    endpoint, _ = await cache.match_path("schools/ccd/nonexistent/2022/")
    assert endpoint is None


async def test_suggest_paths_offers_the_closest_templates(mock_api, cache):
    """An unmatched path becomes a menu, not a dead end."""
    suggestions = await cache.suggest_paths("schools/ccd/directory")
    assert suggestions
    assert any("schools/ccd/directory" in s for s in suggestions)
    # Returned in the copyable form, without the /api/v1 prefix.
    assert not any(s.startswith("/") or s.startswith("api/v1") for s in suggestions)


async def test_get_source_info_and_label(mock_api, cache):
    """get_source_info returns the full source row; get_source_label routes
    through it and returns just the label. Case-insensitive on the code."""
    info = await cache.get_source_info("CCD")
    assert info is not None
    assert info["label"] == "Common Core of Data"
    assert info["link"] == "https://nces.ed.gov/ccd/"

    label = await cache.get_source_label("ccd")
    assert label == "Common Core of Data"

    assert await cache.get_source_info("does-not-exist") is None
    assert await cache.get_source_info(None) is None


def _downloads_rows():
    """Shape mirrors /api-downloads/ for endpoint 25 (schools CCD enrollment):
    a codebook .xls first, then year-partitioned CSVs oldest-first."""
    return [
        {"file_dir": "ccd", "file_name": "codebook_schools_ccd_enrollment.xls",
         "file_label": "Codebook, Schools CCD Enrollment, 1986&ndash;2024",
         "file_size": "36.5 KB"},
        {"file_dir": "ccd", "file_name": "schools_ccd_enrollment_1987.csv",
         "file_label": "Schools CCD Enrollment, 1987", "file_size": "43.6 MB"},
        {"file_dir": "ccd", "file_name": "schools_ccd_enrollment_1988.csv",
         "file_label": "Schools CCD Enrollment, 1988", "file_size": "47.4 MB"},
        {"file_dir": "ccd", "file_name": "schools_ccd_enrollment_2022.csv",
         "file_label": "Schools CCD Enrollment, 2022", "file_size": "894.7 MB"},
    ]


async def test_get_download_urls_excludes_codebook_and_carries_size():
    cache = MetadataCache(client=None)
    cache._downloads_cache[25] = _downloads_rows()

    out = await cache.get_download_urls(25)

    # Codebook .xls is filtered out; only data CSVs survive.
    assert len(out) == 3
    assert all(url.endswith(".csv") for _, url, _ in out)
    label, url, size = out[0]
    assert label == "Schools CCD Enrollment, 1987"
    assert url == (
        "https://educationdata.urban.org/csv/ccd/schools_ccd_enrollment_1987.csv"
    )
    assert size == "43.6 MB"


async def test_get_download_urls_sorts_queried_year_first():
    """Without a year the API's oldest-first order stands, which would offer a
    2022 query three files from the 1980s. Given the year, it leads."""
    cache = MetadataCache(client=None)
    cache._downloads_cache[25] = _downloads_rows()

    assert (await cache.get_download_urls(25))[0][0] == "Schools CCD Enrollment, 1987"

    out = await cache.get_download_urls(25, years=[2022])
    assert out[0][0] == "Schools CCD Enrollment, 2022"
    assert out[0][2] == "894.7 MB"
    # Sort is stable: non-matching files keep their original relative order.
    assert [label for label, _, _ in out[1:]] == [
        "Schools CCD Enrollment, 1987",
        "Schools CCD Enrollment, 1988",
    ]


async def test_get_download_urls_year_with_no_match_preserves_order():
    """Endpoints whose filenames carry no year must not lose the hatch."""
    cache = MetadataCache(client=None)
    cache._downloads_cache[7] = [
        {"file_dir": "meps", "file_name": "schools_meps.csv",
         "file_label": "Schools MEPS", "file_size": "12.1 MB"},
    ]

    out = await cache.get_download_urls(7, years=[2022])
    assert [label for label, _, _ in out] == ["Schools MEPS"]
