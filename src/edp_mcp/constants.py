import os

# Where the server FETCHES from. Overridable because when this runs alongside the
# API in the same compose project it should call the Django container directly
# (EDP_API_BASE_URL=http://web:8000/api/v1/) rather than looping out through
# nginx and back — that skips a network round trip and keeps MCP traffic out of
# the public access logs.
#
# PUBLIC_BASE_URL is deliberately separate and never overridden: provenance
# footers must cite a URL a user can open, which an internal hostname is not.
BASE_URL = os.environ.get("EDP_API_BASE_URL", "https://educationdata.urban.org/api/v1/")
PUBLIC_BASE_URL = "https://educationdata.urban.org/api/v1/"
DOCUMENTATION_URL = "https://educationdata.urban.org/documentation/"

# Bulk CSV files are served from /csv/{file_dir}/{file_name} (nginx → S3).
CSV_DOWNLOAD_BASE = "https://educationdata.urban.org/csv/"

# NOTE: Code→label mappings (special values, state names, and all other coded
# fields) are NOT hardcoded. They are sourced per-variable from the API
# metadata (the `format` of each variable + the /api-values/ endpoint), because
# the meaning of a code is field-specific — e.g. grade=-1 means "Pre-K", not
# "Missing". See server._apply_labels.

# Rate limiting
# Observed EDP response times range from 0.1s to ~29s for identical requests —
# a 10,000-row page is ~12 MB and the API's latency is highly variable. 30s sat
# just under the worst observed case and produced spurious ReadTimeouts.
REQUEST_TIMEOUT = 90  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds, doubles each retry
PAGE_DELAY = 0.1  # seconds between paginated requests

# ---------------------------------------------------------------------------
# Response size budget
# ---------------------------------------------------------------------------
# Results are returned COMPLETE or not at all. A partial result set is only safe
# when the rows that were dropped are irrelevant to the question, and the server
# cannot know that — so a query whose full result exceeds the budget is refused
# with the row count and a way to narrow it, rather than silently truncated.
# This is the same stance the API itself takes: /summaries/ answers an
# over-large grouping with HTTP 413 rather than a partial aggregate.
#
# A cap on how much text one tool response may return. 
MAX_RESPONSE_CHARS = 60_000

# Rows pulled into memory before the size decision is made. One value covers
# data and summary endpoints alike: the API's page size is fixed at 10,000 and
# it ignores limit/page_size, so a page is the smallest unit of transfer, and
# anything that would exceed MAX_RESPONSE_CHARS is refused regardless. A result
# short of its reported `count` is treated as incomplete and refused, so this
# cap can never silently truncate.
MAX_FETCH_ROWS = 10000
# Rows to accept when scanning a directory to resolve a name. One state is at most
# ~10.5K schools (CA) and the whole college directory ~7K, so this has headroom;
# hitting it means something changed upstream, and the caller is told.
DIRECTORY_SCAN_MAX_ROWS = 50000
# Directory scans held in memory at once. Each is a whole state (California is
# ~10.5K rows), so this cache is bounded where the metadata caches are not.
DIRECTORY_CACHE_ENTRIES = 4

# Wall-clock ceiling for one paginated fetch, across all its pages and retries.
# A request can legitimately take ~29s and retries triple that, so a multi-page
# scan can outlive the MCP client's own timeout and return nothing at all. A
# fetch that runs out of time stops early and reports fewer rows than `count`,
# which every caller already treats as incomplete — so the partial result is
# refused or flagged rather than mistaken for a whole one.
PAGINATION_DEADLINE = 180  # seconds
