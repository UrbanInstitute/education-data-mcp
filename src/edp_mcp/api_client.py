import asyncio
import time
from urllib.parse import parse_qsl

import httpx

from edp_mcp.constants import (
    BASE_URL,
    MAX_FETCH_ROWS,
    MAX_RETRIES,
    PAGE_DELAY,
    PAGINATION_DEADLINE,
    REQUEST_TIMEOUT,
    RETRY_DELAY,
)


def api_error_message(error: Exception) -> str | None:
    """The portal's own explanation of a failed request, if it sent one.

    The aggregation backend answers a bad query with a precise, single-sentence
    diagnosis — ``[{"API ERROR": "'race' is not a valid 'var' argument"}]`` —
    which `raise_for_status` discards along with the rest of the body. Recovering
    it turns a guess about what went wrong into the actual reason, so a caller
    can fix the query in one step instead of probing. Returns None when the
    response carries no such body (a Django 500 page, for instance).
    """
    response = getattr(error, "response", None)
    if response is None:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return None
    for item in payload:
        if isinstance(item, dict):
            for key, value in item.items():
                if str(key).strip().upper() == "API ERROR" and value:
                    return str(value).strip()
    return None


class EdpClient:
    """Async HTTP client for the Education Data Portal API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
                # Tag all portal traffic as coming from the MCP server. httpx merges
                # these default params into every request, so data, summaries, and
                # metadata calls all carry it. Kept out of the provenance/citation
                # URLs (built separately in server.py) so cited URLs stay clean.
                params={"mode": "mcp"},
                follow_redirects=True,
            )
        return self._client

    @staticmethod
    def _absorb_query(url: str, params: dict | None) -> tuple[str, dict | None]:
        """Move any query string on `url` into `params`.

        The client carries a default `mode=mcp` param, and httpx REPLACES a URL's
        existing query string when the client has default params rather than
        merging with it. Paginated `next` links arrive as fully-formed URLs
        carrying `?page=N` plus the original filters, so handing one to httpx
        untouched drops both — every "next page" request silently refetches
        page 1. Lifting the query into `params` lets httpx merge it correctly.
        """
        if "?" not in url:
            return url, params
        path, _, query = url.partition("?")
        merged = dict(parse_qsl(query, keep_blank_values=True))
        if params:
            merged.update(params)  # explicit params win over ones in the URL
        return path, merged

    async def _request_with_retry(
        self, url: str, params: dict | None = None, retry_server_errors: bool = True
    ) -> dict:
        """Make a GET request with retry logic.

        `retry_server_errors=False` makes a 5xx fail immediately. The aggregation
        endpoints answer an unroutable path with a deterministic 500, so retrying
        one costs three round trips and the backoff between them to reach the
        same answer — a slow dead end where a fast one is recoverable.
        """
        client = await self._get_client()
        url, params = self._absorb_query(url, params)
        delay = RETRY_DELAY

        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                if attempt == MAX_RETRIES - 1:
                    raise
                # Retry on 5xx, 429 (rate limit), and transport errors — but not
                # other 4xx (a 404/400 won't fix itself on retry).
                if isinstance(e, httpx.HTTPStatusError):
                    status = e.response.status_code
                    if status < 500 and status != 429:
                        raise
                    if status >= 500 and not retry_server_errors:
                        raise
                    # Be a courteous client: honor Retry-After on a 429 when present.
                    if status == 429:
                        retry_after = e.response.headers.get("Retry-After", "")
                        if retry_after.isdigit():
                            await asyncio.sleep(min(int(retry_after), 60))
                            continue
                await asyncio.sleep(delay)
                delay *= 2

        # Unreachable while MAX_RETRIES >= 1 (the loop returns or raises), but keep
        # the declared -> dict contract honest for type checkers.
        raise RuntimeError("request retry loop exited without a response")

    def _make_relative(self, url: str) -> str:
        """Strip the base URL from a full URL to make it relative."""
        if url.startswith(BASE_URL):
            return url[len(BASE_URL):]
        return url

    async def _paginate(
        self, url: str, params: dict | None = None, max_rows: int | None = None,
        retry_server_errors: bool = True,
    ) -> tuple[list[dict], int]:
        """Fetch a paginated endpoint. Returns (rows, total_count).

        `total_count` is the API's own `count`, so callers can tell a complete
        result from a capped one. `max_rows` caps accumulation (None = unbounded,
        which is what metadata endpoints want) and is applied to the FIRST page
        too: the API serves up to 10,000 rows per page, so a cap that only
        guarded the loop let a single oversized page through untouched.
        """
        data = await self._request_with_retry(url, params, retry_server_errors)
        total_count = data.get("count", 0)
        rows = data.get("results", [])
        if max_rows is not None:
            rows = rows[:max_rows]
        rows = list(rows)

        seen: set[str] = set()
        deadline = time.monotonic() + PAGINATION_DEADLINE
        while data.get("next") and (max_rows is None or len(rows) < max_rows):
            # Out of time: stop with what we have. `count` still reports the true
            # total, so the short result reads as incomplete to every caller.
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(PAGE_DELAY)
            next_url = self._make_relative(data["next"])
            # Never fetch the same page twice. Re-serving a page appends duplicate
            # rows and, when max_rows is None, loops forever; bail instead.
            if next_url in seen:
                break
            seen.add(next_url)
            data = await self._request_with_retry(next_url)
            page = data.get("results", [])
            if not page:
                break
            rows.extend(page)

        if max_rows is not None:
            rows = rows[:max_rows]
        return rows, (total_count or len(rows))

    async def _fetch_all(self, url: str, params: dict | None = None) -> list[dict]:
        """Every row of a metadata endpoint."""
        rows, _ = await self._paginate(url, params)
        return rows

    async def get_endpoints(self, **filters: str | int) -> list[dict]:
        """Fetch all endpoint definitions from /api-endpoints/."""
        params = {k: v for k, v in filters.items() if v is not None}
        return await self._fetch_all("api-endpoints/", params or None)

    async def get_endpoint_varlist(self, endpoint_id: int) -> list[dict]:
        """Fetch variable list for a specific endpoint from /api-endpoint-varlist/."""
        return await self._fetch_all(
            "api-endpoint-varlist/", {"endpoint_id": endpoint_id}
        )

    async def get_values(self, format_name: str) -> list[dict]:
        """Fetch code-to-label mappings from /api-values/."""
        return await self._fetch_all(
            "api-values/", {"format_name": format_name}
        )

    async def get_sources(self) -> list[dict]:
        """Fetch data source descriptions from /api-sources/."""
        return await self._fetch_all("api-sources/")

    async def get_changes(self) -> list[dict]:
        """Fetch the changelog/version history from /api-changes/.

        Used to determine the current EDP version for citations. Note the
        records are NOT ordered newest-first — callers must pick by release_date.
        """
        return await self._fetch_all("api-changes/")

    async def get_downloads(self, endpoint_id: int) -> list[dict]:
        """Fetch bulk-download file listings for an endpoint from /api-downloads/."""
        return await self._fetch_all(
            "api-downloads/", {"endpoint_id": endpoint_id}
        )

    async def fetch_summary(
        self, path: str, var: str, stat: str, by: str, filters: dict | None = None
    ) -> tuple[list[dict], int]:
        """Fetch aggregated data from a summary endpoint.

        Args:
            path: Base path like "schools/ccd/enrollment"
            var: Variable to aggregate (e.g., "enrollment")
            stat: Statistic to compute (sum, count, avg, min, max, variance, stddev, median)
            by: Comma-separated grouping variables (e.g., "fips,race")
            filters: Optional filters (e.g., {"fips": "6", "charter": "1"})

        Returns:
            (records, total_count). `total_count` is the API's own `count`, so
            the caller can tell a complete result from a capped one and refuse
            rather than emit a partial aggregate.
        """
        params = {"var": var, "stat": stat, "by": by}
        if filters:
            params.update(filters)
        return await self._paginate(
            f"{path}/summaries", params, max_rows=MAX_FETCH_ROWS,
            retry_server_errors=False,
        )

    async def fetch_data(
        self, path: str, params: dict | None = None, max_records: int = MAX_FETCH_ROWS
    ) -> tuple[list[dict], int]:
        """Fetch data rows from an endpoint. Returns (records, total_count)."""
        return await self._paginate(path, params, max_rows=max_records)
