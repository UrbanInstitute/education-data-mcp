"""Tests for the pure formatting helpers — surfacing variable definitions,
the dataset header (endpoint + source + sample code), and text cleaning."""

from edp_mcp.formatting import (
    _clean_code,
    _clean_description,
    format_data,
    format_dataset_header,
    format_endpoints,
    format_too_large,
    format_variables,
)


def test_format_endpoints_decodes_entities_and_html():
    endpoints = [{
        "endpoint_id": 26,
        "endpoint_url": "/api/v1/schools/ccd/enrollment/{year}/{grade}/race/",
        "years_available": "1986&ndash;2024",
        "description": "Membership by grade &amp; race.<br/>Operational schools only.",
    }]
    out = format_endpoints(endpoints)
    assert "1986–2024" in out
    assert "&ndash;" not in out and "&amp;" not in out and "<br" not in out
    assert "Membership by grade & race." in out


def test_clean_description_strips_br_and_entities():
    raw = "Line one.<br/><br/>\n\nLine two &ndash; with an &amp; entity."
    out = _clean_description(raw)
    assert "<br" not in out
    assert "&ndash;" not in out and "&amp;" not in out
    assert "–" in out and "&" in out
    # No run of 3+ blank lines survives the collapse.
    assert "\n\n\n" not in out


def test_clean_description_separates_list_items():
    """<ul>/<li> markup must not run items together (real CRDC source text)."""
    raw = (
        "The following endpoints are currently available:"
        "<ul><li>Directory</li><li>Enrollment</li><li>Discipline</li></ul>"
    )
    out = _clean_description(raw)
    assert "<li>" not in out and "<ul>" not in out
    # Items stay separated instead of collapsing to "DirectoryEnrollment".
    assert "DirectoryEnrollment" not in out
    assert "available:Directory" not in out
    for item in ("Directory", "Enrollment", "Discipline"):
        assert item in out
    # Consecutive </li><li> boundaries collapse to a single break, not double.
    assert "\n\n" not in out


def test_clean_description_skips_none_sentinel():
    assert _clean_description("None") == ""
    assert _clean_description(None) == ""
    assert _clean_description("") == ""
    assert _clean_description("   ") == ""


def test_clean_code_unescapes():
    raw = 'get_education_data(level = \\"schools\\",\\nsource = \\"ccd\\")'
    out = _clean_code(raw)
    assert '\\"' not in out and '\\n' not in out
    assert '"schools"' in out
    assert out.count("\n") == 1  # the \n became a real newline
    assert _clean_code("None") == ""


def test_format_variables_shows_definition():
    variables = [{"variable": "enrollment", "label": "Count",
                  "description": "Total student membership."}]
    out = format_variables(variables)
    assert "Definition: Total student membership." in out


def test_format_variables_skips_none_description():
    """A literal 'None' description must not print an empty Definition line."""
    variables = [{"variable": "x", "label": "X", "description": "None"}]
    out = format_variables(variables)
    assert "Definition:" not in out


def test_format_variables_cleans_html_description():
    variables = [{"variable": "race", "label": "Race/ethnicity",
                  "description": "Varies by year.<br/><br/>CCD uses seven since 2008."}]
    out = format_variables(variables)
    assert "<br" not in out
    assert "Definition: Varies by year." in out
    assert "CCD uses seven since 2008." in out


def test_format_dataset_header_shows_endpoint_source_and_code():
    endpoint = {
        "endpoint_id": 26,
        "endpoint_url": "/api/v1/schools/ccd/enrollment/{year}/{grade}/race/",
        "years_available": "1986&ndash;2024",
        "description": "Membership by grade and race.",
        "r_code": 'get_education_data(level = \\"schools\\")',
        "stata_code": 'educationdata using \\"school ccd enrollment race\\"',
    }
    source_info = {
        "data_source": "ccd",
        "label": "Common Core of Data",
        "description": "The US Department of Education's primary K-12 database.",
        "link": "https://nces.ed.gov/ccd/",
    }
    out = format_dataset_header(endpoint, source_info)
    assert "Dataset: schools/ccd/enrollment/{year}/{grade}/race" in out
    assert "1986–2024" in out  # entity decoded in years_available
    assert "Membership by grade and race." in out
    assert "Common Core of Data (CCD)" in out
    assert "primary K-12 database" in out
    assert "https://nces.ed.gov/ccd/" in out
    assert "get_education_data(level = \"schools\")" in out  # unescaped
    assert "educationdata using" in out


def test_format_dataset_header_omits_missing_fields():
    """None/absent optional fields are dropped, never printed as 'None'."""
    endpoint = {
        "endpoint_id": 5,
        "endpoint_url": "/api/v1/schools/meps/{year}/",
        "description": "None",
        "r_code": "None",
        "stata_code": "None",
    }
    out = format_dataset_header(endpoint, None)
    assert "Dataset: schools/meps/{year}" in out
    assert "None" not in out
    assert "Sample code" not in out
    assert "Source:" not in out


def _records():
    return [{"ncessch": "480000101146", "enrollment": 295}]


def test_format_data_emits_csv_not_json_lines():
    """CSV because JSON-lines repeats every field name on every row — ~3x the
    tokens for identical information on a wide table."""
    out = format_data(_records(), total_count=1)
    assert "ncessch,enrollment" in out
    assert "480000101146,295" in out
    assert '{"ncessch"' not in out


def test_format_data_states_the_result_is_complete():
    """Callers size the query before formatting, so anything formatted is whole.
    Saying so is what makes counting/ranking over the rows safe."""
    out = format_data(_records(), total_count=1)
    assert "Returned 1 record(s) — complete result set." in out


def test_format_data_projects_to_requested_fields():
    records = [{"ncessch": "48", "enrollment": 295, "school_name": "Lamar"}]
    out = format_data(records, total_count=1, fields=["ncessch", "enrollment"])
    assert "ncessch,enrollment" in out
    assert "school_name" not in out
    assert "Lamar" not in out


def test_suppression_note_ignores_columns_that_were_dropped():
    """A negative code in a column the caller never sees is not worth a warning,
    and warning about it misleads about which visible number to distrust."""
    records = [{"ncessch": "48", "enrollment": 295, "free_lunch": -3}]
    assert "Suppressed" not in format_data(
        records, total_count=1, fields=["ncessch", "enrollment"]
    )
    assert "Suppressed" in format_data(
        records, total_count=1, fields=["ncessch", "free_lunch"]
    )


def test_too_large_refusal_is_actionable_and_leaks_no_rows():
    downloads = [
        ("Schools CCD Enrollment, 2022", "https://x/2022.csv", "894.7 MB"),
        ("Schools CCD Enrollment, 1987", "https://x/1987.csv", "43.6 MB"),
    ]
    out = format_too_large(
        what="schools/ccd/enrollment", total_count=9180, estimated_chars=360000,
        narrowing_hint="add fips=… to restrict to one state",
        download_urls=downloads,
    )
    assert "9,180 rows" in out
    assert "360 KB" in out
    assert "add fips=… to restrict to one state" in out
    assert "fields=" in out
    assert "get_summary" in out
    # The bulk CSV is the escape hatch, with size — a 894.7 MB file is a trap
    # unless the caller is told.
    assert "Schools CCD Enrollment, 2022 (894.7 MB): https://x/2022.csv" in out


def test_too_large_caps_downloads_at_three_and_tolerates_missing_size():
    downloads = [(f"File {i}", f"https://x/{i}.csv", "") for i in range(5)]
    out = format_too_large(
        what="x", total_count=9180, estimated_chars=1, narrowing_hint="hint",
        download_urls=downloads,
    )
    assert "File 0: https://x/0.csv" in out  # no empty "()" when size is absent
    assert "File 3" not in out
    assert "…and 2 more (see documentation)" in out
