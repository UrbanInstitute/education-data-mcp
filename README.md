# education-data-mcp

An MCP server over the [Urban Institute's Education Data Portal](https://educationdata.urban.org/documentation/) —
harmonized federal education data (CCD, CRDC, IPEDS, EdFacts, SAIPE, College
Scorecard, MEPS, PSEO) for schools, school districts, and colleges.

Queries the live EDP API. No API key required. Read-only.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Installation

```bash
git clone https://github.com/UrbanInstitute/education-data-mcp.git
cd education-data-mcp
uv sync
```

## Tools

Six tools, following a discovery-first workflow:

| Tool | Purpose |
|------|---------|
| `search_datasets` | Find available datasets by level, source, topic, or keyword |
| `describe_dataset` | Inspect a dataset's variables, filters, and coded value formats |
| `get_data` | Fetch raw data records with human-readable labels (by default) |
| `get_summary` | Get aggregated statistics (counts, sums, averages) by group |
| `lookup_codes` | Translate between codes and labels (e.g., FIPS 6 = California) |
| `resolve_entity` | Find a school/district/college ID by name (e.g., "Harvard" → unitid 166027) |

**Typical workflow**: `search_datasets` → `describe_dataset` → `get_summary` or `get_data`, with `lookup_codes` and `resolve_entity` as needed.

## Tool reference

### search_datasets

Find available datasets. 

| Parameter | Type | Description |
|-----------|------|-------------|
| `level` | string (optional) | `"schools"`, `"school-districts"`, or `"college-university"` |
| `source` | string (optional) | `"ccd"`, `"ipeds"`, `"crdc"`, `"edfacts"`, `"saipe"`, `"scorecard"`, etc. |
| `topic` | string (optional) | `"enrollment"`, `"directory"`, `"finance"`, `"discipline"`, etc. |
| `search` | string (optional) | Keyword to match against dataset URLs and descriptions |

### describe_dataset

Inspect a dataset's variables, filters, and coded value formats.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | Dataset path, raw template or filled in: `"schools/ccd/enrollment/{year}/{grade}/race/"` or `"schools/ccd/enrollment/2022/grade-99/race/"` |
| `filterable_only` | boolean (default: false) | Only show variables usable as query filters |

### get_data

Fetch education data. Returns human-readable labels by default.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | A path with every `{placeholder}` filled in: `"schools/ccd/directory/2022/"`. One year per call |
| `filters` | string (optional) | Query filters: `"fips=11&charter=1"`. Also accepts `"ordering=-enrollment"` to rank server-side |
| `fields` | string (optional) | Columns to return: `"ncessch,school_name,enrollment"` |
| `add_labels` | boolean (default: true) | Decode coded values to human-readable labels |
| `preview` | boolean (default: false) | Return a small labelled sample instead of a complete result |

### get_summary

Get aggregated statistics from the Education Data Portal. Use for counts, totals, or averages across groups. Much faster than fetching raw data and computing yourself.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | The dataset path WITHOUT `{placeholders}` — the static head of the template: `"schools/ccd/enrollment"` |
| `var` | string | Variable to aggregate: `"enrollment"`, `"teachers_fte"`, etc. |
| `stat` | string | Statistic: `"sum"`, `"count"`, `"avg"`, `"min"`, `"max"`, `"median"`, `"stddev"`, `"variance"` |
| `by` | string | Grouping variables (comma-separated): `"fips"`, `"race"`, `"fips,charter"` |
| `filters` | string (optional) | Query filters: `"fips=6"` or `"fips=6&year=2022"`. `ordering=` is not supported here |

Results are always grouped by year in addition to the specified groupings.

**Example**: Total enrollment by state: `get_summary(path="schools/ccd/enrollment", var="enrollment", stat="sum", by="fips")`

### lookup_codes

Look up code-to-label mappings. Use to find filter values (e.g., "California" = FIPS 6) or understand coded results.

| Parameter | Type | Description |
|-----------|------|-------------|
| `format_name` | string | Format name from `describe_dataset` output: `"fips"`, `"race"`, `"sex"`, etc. |
| `codes` | string (optional) | Comma-separated codes: `"1,2,3"`. Omit to see all values. |

**Coded values are field-specific.** Code meanings come from each variable's API metadata, not a fixed table, and `get_data`/`get_summary` decode them to labels automatically. Negative codes _often_ mean `-1` = Missing/not reported, `-2` = Not applicable, `-3` = Suppressed for privacy — but not always: for `grade`, `-1` means **Pre-K** (`0` = Kindergarten). Use `lookup_codes` to see a variable's full, authoritative code list.

### resolve_entity

Resolve a school, district, or college name to its ID for filtering. Use when you need to filter data by a specific entity.

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Name to search for (case-insensitive substring match) |
| `entity_type` | string | `"school"`, `"district"`, or `"college"` |
| `fips` | integer (optional) | State FIPS code (e.g., 6 for California). Required for schools and districts |
| `max_results` | integer (default: 15) | Maximum number of results to return |

Returns matching entities with their IDs (`ncessch` for schools, `leaid` for districts, `unitid` for colleges) for use in `get_data` filters. 

---

## Running it

### MCP Inspector (interactive testing)

```bash
uv run mcp dev src/edp_mcp/server.py
```

Opens a browser at `http://localhost:6274` — connect, open **Tools**, and run
any tool with parameters.

### Claude Desktop / Claude Code / VS Code / Copilot CLI

```json
{
  "mcpServers": {
    "edp-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/education-data-mcp", "edp-mcp"]
    }
  }
}
```

| Client | Where it goes |
|---|---|
| Claude Desktop | `claude_desktop_config.json` — macOS: `~/Library/Application Support/Claude/`; Windows: `%APPDATA%\Claude\` |
| Claude Code | `.claude/settings.json`, or `claude mcp add edp-mcp -- uv run --directory /absolute/path/to/education-data-mcp NAME` |
| VS Code (Copilot) | `.vscode/settings.json`, nested as `{"mcp": {"servers": {...}}}` |

### stdio (direct)

```bash
uv run edp-mcp
```

### Streamable HTTP (hosted)

```bash
MCP_TRANSPORT=streamable-http PORT=8080 uv run edp-mcp
```

MCP is served at `POST /mcp`; `GET /health` is a plain unauthenticated health
check for a load balancer or orchestrator.

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `PORT` | `8080` | Listen port |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_ALLOWED_HOSTS` | _(unset)_ | Comma-separated `Host` allow-list |
| `MCP_ALLOWED_ORIGINS` | _(unset)_ | Comma-separated `Origin` allow-list |

**Set `MCP_ALLOWED_HOSTS` to the public hostname before exposing this beyond a
private network.** Both are unset by default, which leaves the SDK's
DNS-rebinding protection off — setting either turns it on. Note that enabling it
with an allow-list that omits the real hostname rejects every request.

## Hosted

Also available as a hosted streamable-HTTP server:
[https://educationdata.urban.org/mcp/edp](https://educationdata.urban.org/mcp/edp)

## Tests

```bash
uv run pytest -q
```

Tests use saved API fixtures (`tests/fixtures/`) with mocked HTTP, so no network
is needed. `scripts/smoke_live.py` exercises the real API end to end.
