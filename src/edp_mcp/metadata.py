import datetime
import html
import re
from collections.abc import Iterable

from edp_mcp.api_client import EdpClient
from edp_mcp.constants import (
    CSV_DOWNLOAD_BASE,
    DIRECTORY_CACHE_ENTRIES,
    DIRECTORY_SCAN_MAX_ROWS,
)


def _path_segments(path: str) -> list[str]:
    """Split an endpoint URL or request path into comparable segments.

    Tolerates the shapes a caller might paste: with or without the /api/v1
    prefix, a leading slash, or a trailing slash.
    """
    cleaned = (path or "").strip().strip("/")
    if cleaned.startswith("api/v1/"):
        cleaned = cleaned[len("api/v1/"):]
    return [s for s in cleaned.split("/") if s]


def _clean_path(endpoint_url: str) -> str:
    """An endpoint template as callers should pass it: no /api/v1, no slashes."""
    return "/".join(_path_segments(endpoint_url))


# What each path placeholder is allowed to contain. Without this a wildcard
# swallows any segment, so a path that is simply missing one — say
# "…/enrollment/2020/race/" with no grade — matches the {year}/{grade} template
# with grade="race" and fires a request for a dataset the caller never asked
# for. Constraining the wildcards turns that into a clean no-match.
_PLACEHOLDER_PATTERNS = {
    "year": re.compile(r"^\d{4}$"),
    "grade": re.compile(r"^grade-[\w-]+$", re.IGNORECASE),
    "grade_edfacts": re.compile(r"^grade-[\w-]+$", re.IGNORECASE),
    # level_of_study values are words or the 99 total code; anything without a
    # slash is plausible, so this stays permissive rather than guessing a list.
    "level_of_study": re.compile(r"^[\w-]+$"),
}


# How to fill each placeholder. The path segment is NOT the same as the variable
# value — grade 9 is the segment "grade-9" — which is the single likeliest way a
# caller gets a path wrong, so the hint has to travel with every failure.
PLACEHOLDER_HINTS = {
    "year": "a four-digit year, e.g. 2022 (the fall of the academic year)",
    "grade": (
        "a grade SEGMENT, not a bare number: grade-pk, grade-k, grade-1 … "
        "grade-12, or grade-99 for all grades"
    ),
    "grade_edfacts": (
        "a grade SEGMENT, not a bare number: grade-3 … grade-12, or grade-99 "
        "for all grades"
    ),
    "level_of_study": (
        "a level segment, e.g. undergraduate, graduate, first-professional, or "
        "99 for all levels"
    ),
}


# --- summary endpoint paths -------------------------------------------------
#
# Summary endpoints are a SEPARATE surface from the data endpoints: 91 routes
# hand-registered in the portal's urls.py that proxy to an Athena backend. No
# metadata endpoint publishes them, so the path has to be derived from the data
# endpoint's template — the same derivation the documentation site performs
# (education-api-documentation/js/script.js), reproduced here rather than
# guessed at. Verified to reproduce all 91 registrations exactly, with no path
# that is not a real endpoint.
#
# Templates whose summary lives under a static SUBTOPIC segment that follows the
# {year} placeholder, so the plain static head is not a valid summary path.
_SUMMARY_SUBTOPICS = {
    ("schools", "crdc", "harassment-or-bullying"): ("allegations", "students"),
    ("schools", "crdc", "restraint-and-seclusion"): ("instances", "students"),
    ("college-university", "ipeds", "fall-enrollment"): ("age", "race", "residence"),
    ("college-university", "scorecard", "student-characteristics"): (
        "aid-applicants", "home-neighborhood",
    ),
}

# Sources whose templates carry no topic segment at all (school-districts/saipe,
# schools/meps), so the segment after the source is already {year}.
_SUMMARY_SOURCE_ONLY = {"saipe", "meps"}

# The only two summary endpoints registered under a different name than their
# data endpoint. Not derivable from any published metadata — the S3 table names
# in api-data-inventory carry internal aliases for ~40 endpoints, so they cannot
# distinguish a real rename from a cosmetic one.
_SUMMARY_RENAMES = {
    "college-university/campus-crime/hate-crimes": "college-university/csafety/hate-crimes",
    "schools/crdc/directory": "schools/crdc/school-characteristics",
}


def summary_path_for(endpoint_url: str) -> str:
    """The /summaries/ path serving a data endpoint template.

    "schools/ccd/enrollment/{year}/{grade}/race/" -> "schools/ccd/enrollment"
    ".../ipeds/fall-enrollment/{year}/{level_of_study}/race/sex/"
        -> "college-university/ipeds/fall-enrollment/race"

    {level_of_study} is dropped before parsing: it splits sibling templates that
    share one summary endpoint, so leaving it in would shift every later segment.
    """
    segments = _path_segments(endpoint_url.replace("/{level_of_study}", ""))

    def at(i: int) -> str:
        return segments[i] if i < len(segments) else ""

    section, source = at(0), at(1)
    if source in _SUMMARY_SOURCE_ONLY:
        topic, subtopic_segment = "", at(3)
    else:
        topic, subtopic_segment = at(2), at(4)

    subtopic = ""
    known = _SUMMARY_SUBTOPICS.get((section, source, topic))
    if known:
        # The segment after {year} names the variant when it is one of the known
        # ones; every other template in the family rolls up to "students", which
        # is the variant carrying the per-student breakdowns.
        subtopic = subtopic_segment if subtopic_segment in known else known[-1]

    parts = [p for p in (section, source, topic, subtopic) if p]
    path = "/".join(parts)
    return _SUMMARY_RENAMES.get(path, path)


def _unique(names: Iterable[str]) -> list[str]:
    """De-duplicate, keeping first appearance.

    Summary pools are built from the whole family of templates sharing one
    summary endpoint, which repeats every shared variable once per sibling — CRDC
    AP exams alone lists its six measures three times over.
    """
    return list(dict.fromkeys(names))


def summary_var_pool(variables: list[dict]) -> list[str]:
    """Variables valid as get_summary's `var`: numeric-format, and not filters.

    The aggregation backend rejects anything else outright, so this is the whole
    menu — for several endpoints it holds exactly one entry.

    Keyed on `format`, which is what the backend actually tests, rather than on
    `data_type`: the two disagree on real endpoints in both directions. Campus
    crime stores its counts as `format=numeric` with a non-integer data_type —
    filtering on data_type would hide every variable worth summarizing there.
    """
    return _unique(
        str(v["variable"]) for v in variables
        if v.get("variable")
        and str(v.get("is_filter", "0")) != "1"
        and str(v.get("format", "")).lower() == "numeric"
    )


def summary_by_pool(variables: list[dict]) -> list[str]:
    """Variables valid as get_summary's `by`: the filters, minus `year`.

    `year` is excluded because every summary is grouped by it automatically —
    passing it adds nothing.
    """
    return _unique(
        str(v["variable"]) for v in variables
        if v.get("variable")
        and str(v.get("is_filter", "0")) == "1"
        and v["variable"] != "year"
    )


def _placeholder_accepts(name: str, value: str) -> bool:
    # The un-filled template itself matches, so describe_dataset can be handed
    # the path exactly as search_datasets printed it.
    if value == "{" + name + "}":
        return True
    pattern = _PLACEHOLDER_PATTERNS.get(name)
    return pattern.match(value) is not None if pattern else True


class MetadataCache:
    """Lazy-loading cache for Education Data Portal metadata."""

    def __init__(self, client: EdpClient) -> None:
        self._client = client
        self._endpoints: list[dict] | None = None
        self._varlist_cache: dict[int, list[dict]] = {}
        self._values_cache: dict[str, list[dict]] = {}
        self._sources: dict[str, dict] | None = None
        self._version: str | None = None
        self._downloads_cache: dict[int, list[dict]] = {}
        self._directory_cache: dict[tuple, tuple[list[dict], int]] = {}
        # Caches live for the process lifetime. That's fresh-per-session for stdio,
        # and for a long-running hosted server the refresh is a redeploy/restart —
        # EDP metadata only changes on (infrequent) versioned releases.

    async def get_endpoints(self) -> list[dict]:
        """Get all endpoints, loading from API on first call."""
        if self._endpoints is None:
            self._endpoints = await self._client.get_endpoints()
        return self._endpoints

    async def get_endpoint_varlist(self, endpoint_id: int) -> list[dict]:
        """Get variable list for an endpoint, caching after first fetch."""
        if endpoint_id not in self._varlist_cache:
            rows = await self._client.get_endpoint_varlist(endpoint_id)
            self._varlist_cache[endpoint_id] = self._dedupe_varlist(rows)
        return self._varlist_cache[endpoint_id]

    @staticmethod
    def _dedupe_varlist(variables: list[dict]) -> list[dict]:
        """Collapse the duplicate rows the API returns for a single variable.

        Some endpoints (e.g. CRDC harassment) return two rows per variable: one
        carrying the real ``description`` and one a bare ``"None"`` stub — but the
        stub often holds fields the rich row lacks (e.g. the suppression-code
        ``values``). Merge field-by-field, keeping the first meaningful value for
        each field, so the model sees one complete row instead of a
        defined/undefined pair. First appearance sets row order; rows without a
        ``variable`` name pass through untouched.
        """
        def meaningful(v: object) -> bool:
            return v is not None and str(v).strip().lower() not in ("", "none")

        merged: dict[str, dict] = {}
        order: list[str] = []
        for i, row in enumerate(variables):
            name = row.get("variable")
            # str(): `merged` is keyed by str, and `meaningful` accepts any
            # object, so without the coercion the key type is unconstrained.
            # A no-op for the string names the metadata actually carries.
            key = str(name) if meaningful(name) else f"__row_{i}"
            if key not in merged:
                merged[key] = dict(row)
                order.append(key)
            else:
                base = merged[key]
                for field, value in row.items():
                    if meaningful(value) and not meaningful(base.get(field)):
                        base[field] = value
        return [merged[k] for k in order]

    async def get_values(self, format_name: str) -> list[dict]:
        """Get code-to-label mappings for a format, caching after first fetch."""
        if format_name not in self._values_cache:
            self._values_cache[format_name] = await self._client.get_values(format_name)
        return self._values_cache[format_name]

    async def get_directory(
        self, path: str, year: int, fields: list[str], fips: int | None = None
    ) -> tuple[list[dict], int]:
        """Directory rows for a name search, cached per (dataset, year, state).

        This is the most expensive fetch the server makes — one state's school
        directory is megabytes — and a session that resolves several names in the
        same state would otherwise re-download it every time. A failed fetch
        raises and caches nothing, so a transient error is retried, not sticky.
        """
        key = (path, year, fips, tuple(fields))
        if key not in self._directory_cache:
            params: dict[str, str | int] = {"fields": ",".join(fields)}
            if fips:
                params["fips"] = fips
            rows = await self._client.fetch_data(
                f"{path}/{year}/", params=params, max_records=DIRECTORY_SCAN_MAX_ROWS
            )
            # Bounded, unlike the metadata caches: entries here are whole state
            # directories (California is ~10.5K rows), so an unbounded one would
            # grow without limit on a long-running hosted server. Oldest out
            # first — a session works one state at a time, so the recent entries
            # are the ones worth keeping.
            while len(self._directory_cache) >= DIRECTORY_CACHE_ENTRIES:
                self._directory_cache.pop(next(iter(self._directory_cache)))
            self._directory_cache[key] = rows
        return self._directory_cache[key]

    async def get_source_info(self, source_code: str) -> dict | None:
        """Return the full /api-sources/ row for a source code (label,
        description, link), or None if not found.

        Backs both get_source_label (footer) and describe_dataset's source
        header. Never invents a row; callers fall back to the bare code.
        """
        if source_code is None:
            return None
        if self._sources is None:
            rows = await self._client.get_sources()
            self._sources = {
                (r.get("data_source") or "").lower(): r for r in rows
            }
        return self._sources.get(source_code.lower())

    async def get_source_label(self, source_code: str) -> str | None:
        """Return the full human-readable name for a source code (ccd → 'Common
        Core of Data'), pulled from /api-sources/. None if not found.

        Used only for the provenance footer — never invents a name; if the
        source row is missing the caller falls back to the bare code.
        """
        row = await self.get_source_info(source_code)
        return row.get("label") if row else None

    async def get_version(self) -> str | None:
        """Return the current EDP version string for citations.

        Picks the /api-changes/ record with the latest release_date (the records
        are NOT newest-first; results[0] is the oldest 0.1.0 beta). Returns None
        on any failure — callers must omit the version rather than guess.
        """
        if self._version is not None:
            return self._version
        try:
            rows = await self._client.get_changes()
        except Exception:
            return self._version
        if not rows:
            return self._version

        def parse_date(row: dict) -> datetime.datetime:
            try:
                return datetime.datetime.strptime(
                    row.get("release_date", ""), "%m/%d/%Y"
                )
            except (ValueError, TypeError):
                return datetime.datetime.min

        latest = max(rows, key=parse_date)
        self._version = latest.get("version")
        return self._version

    async def get_download_urls(
        self, endpoint_id: int, years: list[int] | None = None
    ) -> list[tuple[str, str, str]]:
        """Return (label, url, size) for each bulk DATA CSV of an endpoint.

        URLs are constructed only from real api-downloads fields
        (CSV_DOWNLOAD_BASE + file_dir/file_name). Codebook .xls files are
        excluded. Empty list if the endpoint has no downloads.

        Most endpoints partition their bulk CSVs by year, and api-downloads
        returns them oldest-first — so callers that show only the first few would
        offer 1987 files to someone querying 2022. Given `years`, matching files
        sort to the front. Sorting rather than filtering leaves endpoints whose
        filenames carry no year in their original order.
        """
        if endpoint_id not in self._downloads_cache:
            try:
                self._downloads_cache[endpoint_id] = await self._client.get_downloads(
                    endpoint_id
                )
            except Exception:
                self._downloads_cache[endpoint_id] = []

        rows = []
        for d in self._downloads_cache[endpoint_id]:
            file_name = d.get("file_name", "") or ""
            file_dir = d.get("file_dir", "") or ""
            if not file_name.lower().endswith(".csv") or not file_dir:
                continue
            label = html.unescape(d.get("file_label") or file_name)
            url = f"{CSV_DOWNLOAD_BASE}{file_dir}/{file_name}"
            size = (d.get("file_size") or "").strip()
            rows.append((file_name, (label, url, size)))

        if years:
            wanted = tuple(str(y) for y in years)
            # Stable sort: matches lead, api-downloads order holds within each group.
            rows.sort(key=lambda r: 0 if any(y in r[0] for y in wanted) else 1)

        return [entry for _, entry in rows]

    async def filter_endpoints(
        self,
        level: str | None = None,
        source: str | None = None,
        topic: str | None = None,
        search: str | None = None,
    ) -> list[dict]:
        """Filter endpoints by level (section), source (class_name), topic, or search term."""
        endpoints = await self.get_endpoints()
        results = endpoints

        if level:
            # section values: "Schools", "School_districts", "College_university"
            # User passes: "schools", "school-districts", "college-university"
            level_normalized = level.lower().replace("-", "_")
            results = [
                e for e in results
                if (e.get("section") or "").lower().replace("-", "_") == level_normalized
            ]

        if source:
            # class_name values: "CCD", "IPEDS", "CRDC", "Campus Crime", etc.
            # Normalize hyphens/underscores to spaces for comparison so that
            # source="campus-crime" matches class_name="Campus Crime".
            def _norm(s: str) -> str:
                return s.upper().replace("-", " ").replace("_", " ")
            source_norm = _norm(source)
            results = [
                e for e in results
                if _norm(e.get("class_name") or "") == source_norm
            ]

        if topic:
            # topic can be None for some endpoints (e.g., directory endpoints)
            # Also match against the URL path for endpoints where topic is None
            topic_lower = topic.lower()
            results = [
                e for e in results
                if (e.get("topic") or "").lower() == topic_lower
                or (e.get("topic") is None and topic_lower in (e.get("endpoint_url") or "").lower())
            ]

        if search:
            tokens = search.lower().split()

            def blob_of(e: dict) -> str:
                return (
                    (e.get("endpoint_url") or "") + " "
                    + (e.get("description") or "") + " "
                    + (e.get("topic") or "") + " "
                    + (e.get("sub_topic") or "")
                ).lower()

            matched = [e for e in results if all(t in blob_of(e) for t in tokens)]

            if not matched and len(tokens) > 1:
                # A strict AND over every token is brittle against phrases whose
                # words never appear together verbatim (e.g. "8th" vs. "grade
                # 8"). Rank by how many tokens hit instead of failing outright,
                # so a near-miss phrase still surfaces the closest datasets.
                scored = [
                    (e, sum(1 for t in tokens if t in blob_of(e)))
                    for e in results
                ]
                scored = [pair for pair in scored if pair[1] > 0]
                scored.sort(key=lambda pair: -pair[1])
                matched = [e for e, _ in scored]

            results = matched

        return results

    async def match_path(self, path: str) -> tuple[dict | None, dict[str, str]]:
        """Match a concrete request path against the endpoint templates.

        Returns (endpoint, path_params) — e.g. for
        "schools/ccd/enrollment/2022/grade-99/race/" against the template
        "/api/v1/schools/ccd/enrollment/{year}/{grade}/race/" it returns that
        endpoint plus {"year": "2022", "grade": "grade-99"}.

        Matching a path the caller already holds is a lookup, where inferring
        one from level/source/topic/subtopic would be a prefix-matching guess
        that can settle on a neighbouring dataset. Segment counts must match
        exactly, so a wrong path fails instead of resolving to something
        plausible.
        """
        segments = _path_segments(path)
        for endpoint in await self.get_endpoints():
            template = _path_segments(endpoint.get("endpoint_url", ""))
            if len(template) != len(segments):
                continue
            params: dict[str, str] = {}
            # strict=: the length guard above makes these equal by construction.
            for want, got in zip(template, segments, strict=True):
                if want.startswith("{") and want.endswith("}"):
                    name = want[1:-1]
                    if not _placeholder_accepts(name, got):
                        break
                    params[name] = got
                elif want.lower() != got.lower():
                    break
            else:
                return endpoint, params
        return None, {}

    async def match_summary_path(self, path: str) -> list[dict]:
        """Every endpoint a summary path refers to, shortest template first.

        Several data endpoints share one summary endpoint — enrollment by grade,
        by grade+race, by grade+sex all aggregate through the same /summaries/
        path — so this returns the whole family rather than guessing one: the
        caller validates against their combined variables (catching a typo
        without rejecting a field that is only valid on a sibling) and takes the
        first as the representative for labels and provenance.

        Matching is on the DERIVED summary path, not on the template's static
        head. The two differ for the endpoints in _SUMMARY_SUBTOPICS and
        _SUMMARY_RENAMES, where the head is not a real endpoint and requesting it
        returns a bare 500.
        """
        wanted = "/".join(_path_segments(path)).lower()
        if not wanted:
            return []
        matches = [
            e for e in await self.get_endpoints()
            if summary_path_for(e.get("endpoint_url", "")).lower() == wanted
        ]
        matches.sort(key=lambda e: len(_path_segments(e.get("endpoint_url", ""))))
        return matches

    async def summary_paths(self) -> list[str]:
        """Every distinct summary path the portal serves, sorted."""
        return sorted({
            summary_path_for(e.get("endpoint_url", ""))
            for e in await self.get_endpoints()
        })

    async def summary_join_varlist(self, endpoint: dict) -> list[dict]:
        """Variables of the directory table pre-joined onto a summary endpoint.

        Summary tables are built pre-joined against their source's directory
        file, so `by=school_level` and `school_level=3` are valid on
        schools/ccd/enrollment even though that variable lives only in
        schools/ccd/directory. Mirrors the backend's own lookup: the source's
        `directory` endpoint, falling back to `institutional-characteristics`.
        Empty list when the source has neither (saipe, meps, nhgis).
        """
        segments = _path_segments(endpoint.get("endpoint_url", ""))
        if len(segments) < 2:
            return []
        section, source = segments[0], segments[1]
        for topic in ("directory", "institutional-characteristics"):
            for candidate in await self.get_endpoints():
                other = _path_segments(candidate.get("endpoint_url", ""))
                if other[:3] == [section, source, topic]:
                    return await self.get_endpoint_varlist(candidate["endpoint_id"])
        return []

    async def explain_mismatch(self, path: str) -> str | None:
        """Why a path narrowly failed to match, when the reason is a placeholder.

        A path that lines up with a template on every static segment and fails
        only on a placeholder's FORMAT is the common near-miss — passing "99"
        where "grade-99" is required. Saying so beats handing back a list of
        templates that still read "{grade}", which tells the caller nothing it
        did not already have.
        """
        segments = _path_segments(path)
        for endpoint in await self.get_endpoints():
            template = _path_segments(endpoint.get("endpoint_url", ""))
            if len(template) != len(segments):
                continue
            rejected: tuple[str, str] | None = None
            # strict=: guarded by the length check above, same as match_path.
            for want, got in zip(template, segments, strict=True):
                if want.startswith("{") and want.endswith("}"):
                    name = want[1:-1]
                    if not _placeholder_accepts(name, got):
                        if rejected is not None:
                            rejected = None
                            break
                        rejected = (name, got)
                elif want.lower() != got.lower():
                    rejected = None
                    break
            if rejected:
                name, value = rejected
                hint = PLACEHOLDER_HINTS.get(name, "see search_datasets")
                return (
                    f"'{value}' is not a valid {{{name}}} value. Expected {hint}.\n\n"
                    f"Template: {_clean_path(endpoint.get('endpoint_url', ''))}"
                )
        return None

    async def suggest_paths(self, path: str, limit: int = 12) -> list[str]:
        """Templates sharing the longest static prefix with `path`.

        Turns an unmatched path into a menu rather than a dead end, which is what
        keeps a mistyped path from costing the caller several blind retries.
        """
        segments = _path_segments(path)
        scored = []
        for endpoint in await self.get_endpoints():
            url = endpoint.get("endpoint_url", "")
            template = _path_segments(url)
            shared = 0
            # strict=False: this scores how long a PREFIX matches, so templates and
            # paths of different lengths are the normal case, not an error.
            for want, got in zip(template, segments, strict=False):
                if want.startswith("{") or want.lower() == got.lower():
                    shared += 1
                else:
                    break
            if shared:
                scored.append((shared, _clean_path(url)))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [p for _, p in scored[:limit]]
