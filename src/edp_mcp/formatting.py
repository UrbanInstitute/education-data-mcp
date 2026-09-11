import csv
import html
import io
import re

from edp_mcp.constants import MAX_RESPONSE_CHARS
from edp_mcp.metadata import (
    PLACEHOLDER_HINTS,
    _clean_path,
    summary_by_pool,
    summary_path_for,
    summary_var_pool,
)

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
# List/paragraph boundaries carry meaning: without a separator, stripping them
# runs items together ("available:<ul><li>Directory</li><li>Enrollment</li>" →
# "available:DirectoryEnrollment"). Collapse each run of these tags to a single
# newline so the items stay separated. Matches consecutive tags (</li><li>) as
# one run to avoid double-spacing.
_BLOCK_RE = re.compile(
    r"(?:\s*</?(?:li|ul|ol|p|div|tr|td)\s*/?>\s*)+", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_description(raw: object) -> str:
    """Turn a raw metadata description into plain, readable text.

    The metadata stores prose with `<br/>` breaks, `<ul>/<li>` lists, and HTML
    entities (`&ndash;`, `&amp;`), and uses the literal string ``"None"`` for "no
    description". This surfaces the real text faithfully — it never invents
    content — but drops the markup and the `"None"` sentinel so the model sees
    clean prose or nothing.
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text or text.lower() == "none":
        return ""
    text = _BR_RE.sub("\n", text)  # <br/> → newline
    text = _BLOCK_RE.sub("\n", text)  # <li>/<ul>/<p>… boundaries → newline
    text = _TAG_RE.sub("", text)  # drop any other stray tags
    text = html.unescape(text)  # &ndash; → –, &amp; → &
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean_code(raw: object) -> str:
    """Un-escape a stored code snippet (r_code/stata_code) for display.

    The metadata stores these with literal ``\\n`` and ``\\"`` escapes; render
    them as real newlines and quotes. Returns "" for the ``"None"`` sentinel.
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text or text.lower() == "none":
        return ""
    return text.replace("\\n", "\n").replace('\\"', '"').replace("\\'", "'")


def strip_code_prefix(code: object, label: str) -> str:
    """Drop EDP's redundant "1 - " prefix from a code_label.

    The API stores labels already prefixed with their own code ("1 - White"), so
    substituting one into a `race` column yields "1 - White" where the column
    already says what it is. Used by both lookup_codes and data labelling, or the
    same value would read two different ways in one session.
    """
    prefix = f"{code} - "
    return label[len(prefix):] if label.startswith(prefix) else label


def _source_display(source_code: str, source_label: str | None) -> str:
    """'Common Core of Data (CCD)' if label known, else bare 'CCD'."""
    code = (source_code or "").upper()
    if source_label:
        return f"{source_label} ({code})" if code else source_label
    return code or "Unknown source"


def format_provenance_footer(
    source_code: str,
    source_label: str | None,
    years_available: str | None,
    query_years: str | None,
    api_url: str,
    version: str | None,
) -> str:
    """Build the provenance footer block.

    Every field is supplied by the caller from a live API value, a deterministic
    computation, or a verified constant — this function never invents data. Any
    missing optional field is omitted rather than guessed.
    """
    display = _source_display(source_code, source_label)

    years_line = f"Years available: {years_available}" if years_available else None
    if years_line and query_years:
        years_line += f" | This query: {query_years}"

    version_seg = f" v. {version}" if version else ""
    citation = (
        f"{source_label or display}, via Education Data Portal"
        f"{version_seg}, Urban Institute, under the ODC Attribution License."
    )

    lines = ["─────", f"Source: {display} — via Urban Institute Education Data Portal"]
    if years_line:
        lines.append(years_line)
    lines.append("License: Open Data Commons Attribution License (ODC-By v1.0)")
    if api_url:
        lines.append(f"API URL: {api_url}")
    lines.append(f"Cite: {citation}")
    # Server `instructions`are truncated by some MCP clients and dropped entirely 
    # by others, so a directive that lives only there reaches the model unreliably; 
    # this line arrives with the numbers it applies to.  
    lines.append(
        "Attribution is required under ODC-By: name the source alongside these "
        "figures, and reproduce the Cite line above rather than composing one."
    )
    return "\n".join(lines)


def _has_negative_value(records: list[dict], fields: list[str] | None = None) -> bool:
    """True if any EMITTED value is a negative number — a signal that suppressed /
    missing / not-applicable codes are present in the result.

    Restricted to `fields` when the output is projected: warning about
    suppression codes sitting in columns the caller never sees is noise, and it
    misleads about which of the visible numbers to distrust.
    """
    for record in records:
        values = (
            (record.get(f) for f in fields) if fields else record.values()
        )
        for value in values:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if value < 0:
                    return True
    return False


_SUPPRESSION_NOTE = (
    "Note: Negative values may be Suppressed (-3), Missing (-1), or "
    "Not applicable (-2); meanings are field-specific."
)


def _download_note(download_urls: list[tuple[str, str, str]]) -> list[str]:
    """Lines pointing at the complete bulk CSV(s).

    URLs come straight from api-downloads, already ordered so files matching the
    queried year lead. Sizes are shown because these are whole-country,
    whole-year extracts big enough that the choice matters — one year of CCD
    school enrollment is ~900 MB.
    """
    lines = []
    for label, url, size in download_urls[:3]:
        lines.append(f"  {label}{f' ({size})' if size else ''}: {url}")
    if len(download_urls) > 3:
        lines.append(f"  …and {len(download_urls) - 3} more (see documentation)")
    return lines


def format_endpoints(endpoints: list[dict]) -> str:
    """Format a list of endpoints as readable text.

    The path shown is exactly what get_data expects once its {placeholders} are
    filled in — no /api/v1 prefix, no surrounding slashes — so the caller can
    copy it rather than reconstruct it.
    """
    if not endpoints:
        return "No endpoints found matching your criteria."

    lines = [
        f"Found {len(endpoints)} dataset(s). Fill in the {{placeholders}} and pass "
        f"the result to get_data as `path`:\n"
    ]
    # State the fill rules here rather than only in the error: {grade} takes a
    # SEGMENT ("grade-9"), not the bare variable value ("9"), and that is the
    # likeliest way a path comes out wrong.
    shown_placeholders = {
        ph for ep in endpoints
        for ph in re.findall(r"\{(\w+)\}", ep.get("endpoint_url", ""))
    }
    legend = [
        f"    {{{name}}} = {PLACEHOLDER_HINTS[name]}"
        for name in sorted(shown_placeholders) if name in PLACEHOLDER_HINTS
    ]
    if legend:
        lines.extend(legend)
        lines.append("")
    for ep in endpoints:
        # Decode HTML entities ("1986&ndash;2024") so raw markup never reaches
        # output; describe_dataset and the footer already do this.
        years = html.unescape(ep.get("years_available", "") or "")
        desc = _clean_description(ep.get("description"))
        lines.append(f"  {_clean_path(ep.get('endpoint_url', ''))}")
        if years:
            lines.append(f"      Years: {years}")
        if desc:
            lines.append(f"      {desc.replace(chr(10), ' ')}")
        lines.append("")

    return "\n".join(lines)


def format_dataset_header(endpoint: dict, source_info: dict | None) -> str:
    """Render the dataset's own description, its source, and sample code.

    Shown above the variable list in describe_dataset so the model gets the
    "which dataset / which source" steering that only lived in the metadata
    before. Every field is read straight from api-endpoints / api-sources;
    anything empty or `"None"` is omitted rather than guessed.
    """
    lines: list[str] = []

    url = endpoint.get("endpoint_url", "")
    lines.append(f"Dataset: {_clean_path(url)}")

    years = html.unescape(endpoint.get("years_available") or "")
    if years:
        lines.append(f"Years available: {years}")

    ep_desc = _clean_description(endpoint.get("description"))
    if ep_desc:
        lines.append("")
        lines.append(f"Description: {ep_desc.replace(chr(10), chr(10) + '  ')}")

    if source_info:
        label = source_info.get("label") or ""
        code = (source_info.get("data_source") or "").upper()
        src_name = f"{label} ({code})" if label and code else (label or code)
        src_desc = _clean_description(source_info.get("description"))
        link = (source_info.get("link") or "").strip()
        if src_name or src_desc:
            lines.append("")
            lines.append(f"Source: {src_name}" if src_name else "Source:")
            if src_desc:
                lines.append(f"  {src_desc.replace(chr(10), chr(10) + '  ')}")
            if link and link.lower() != "none":
                lines.append(f"  More: {link}")

    r_code = _clean_code(endpoint.get("r_code"))
    stata_code = _clean_code(endpoint.get("stata_code"))
    if r_code or stata_code:
        lines.append("")
        lines.append("Sample code (Urban's educationdata package):")
        if r_code:
            lines.append("  R:")
            lines.extend(f"    {ln}" for ln in r_code.split("\n"))
        if stata_code:
            lines.append("  Stata:")
            lines.extend(f"    {ln}" for ln in stata_code.split("\n"))

    lines.append("")
    lines.append("─────")
    lines.append("")
    return "\n".join(lines)


def format_summary_recipe(
    endpoint: dict, variables: list[dict], join_variables: list[dict]
) -> str:
    """The exact get_summary call this dataset accepts, with both argument pools.

    Written out as a ready-to-run call rather than described, because the two
    pools are not guessable from the variable list below it: `var` and `by` are
    disjoint, several datasets admit exactly one `var`, and the groupings a
    summary allows include the source's directory columns, which appear nowhere
    in this endpoint's variables.
    """
    path = summary_path_for(endpoint.get("endpoint_url", ""))
    if not path:
        return ""

    var_pool = summary_var_pool(variables)
    by_pool = summary_by_pool(variables)
    joined = [b for b in summary_by_pool(join_variables) if b not in by_pool]
    # Directory varlists lead with dozens of vintage-stamped Carnegie
    # classification codes (cc_basic_2000, cc_basic_2010, …). They are valid
    # groupings, but listing them first buries sector and institution_level
    # behind 25 near-duplicates of each other. Stable, so API order otherwise holds.
    joined.sort(key=lambda name: 1 if re.search(r"_\d{4}$", name) else 0)

    lines = ["", "SUMMARIES (aggregate without downloading rows)", ""]
    lines.append(f"  path: {path}")

    if var_pool:
        shown = ", ".join(var_pool[:20]) + (" …" if len(var_pool) > 20 else "")
        sole = " (the only one)" if len(var_pool) == 1 else ""
        lines.append(f"  var : {shown}{sole}")
    else:
        lines.append("  var : none — this dataset has no numeric measure to aggregate")
    if by_pool:
        lines.append(f"  by  : {', '.join(by_pool)}")
    if joined:
        shown = ", ".join(joined[:25]) + (" …" if len(joined) > 25 else "")
        lines.append(f"  by  : also, via the pre-joined directory: {shown}")
    lines.append("  stat: sum, count, avg, min, max, median, stddev, variance")

    if var_pool and by_pool:
        # Group the example by state where possible. The varlist leads with the
        # entity ID (unitid, ncessch), which is the one grouping guaranteed to be
        # refused as too fine — a worked example should run, not demonstrate the
        # failure mode.
        example_by = "fips" if "fips" in by_pool else by_pool[0]
        lines += [
            "",
            f"  get_summary(path='{path}', var='{var_pool[0]}', "
            f"stat='sum', by='{example_by}')",
        ]
    lines.append("")
    lines.append(
        "  Every year is returned in one call — filter years only to narrow a "
        "large result."
    )
    lines.append("")
    return "\n".join(lines)


def format_variables(variables: list[dict], filterable_only: bool = False) -> str:
    """Format a variable list as readable text."""
    if filterable_only:
        variables = [v for v in variables if str(v.get("is_filter", "0")) == "1"]

    if not variables:
        return "No variables found."

    lines = [f"Found {len(variables)} variable(s):\n"]
    for var in variables:
        name = var.get("variable", "?")
        label = var.get("label", "")
        data_type = var.get("data_type", "")
        is_filter = str(var.get("is_filter", "0")) == "1"
        fmt = var.get("format", "")
        values = var.get("values", "")
        description = _clean_description(var.get("description"))

        filter_tag = " [FILTER]" if is_filter else ""
        lines.append(f"  {name}{filter_tag}")
        if label:
            lines.append(f"      Label: {label}")
        if description:
            # Indent continuation lines under the "Definition:" label.
            lines.append(f"      Definition: {description.replace(chr(10), chr(10) + '        ')}")
        if data_type:
            lines.append(f"      Type: {data_type}")
        if fmt and fmt != "None" and fmt != "numeric":
            lines.append(f"      Format: {fmt}")
        if values and values != "None" and values != "{}":
            lines.append(f"      Values: {values}")
        lines.append("")

    return "\n".join(lines)


def records_to_csv(records: list[dict], fields: list[str] | None = None) -> str:
    """Render records as CSV.

    CSV rather than JSON-lines because JSON repeats every field name on every
    row: for a 52-column directory that is ~3x the tokens for identical
    information. Values are written as-is; None becomes an empty cell.
    """
    if not records:
        return ""
    # An explicit `fields` list wins — that is what the caller asked the API for.
    # Otherwise take the union of keys in first-seen order, so a record carrying
    # an extra key does not silently lose it.
    columns = fields or list(dict.fromkeys(k for r in records for k in r))
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for record in records:
        writer.writerow({k: record.get(k) for k in columns})
    return buf.getvalue().rstrip("\n")


def _size(chars: int) -> str:
    """Human-readable size of a rendered response."""
    return f"{chars / 1_000_000:.1f} MB" if chars >= 1_000_000 else f"{chars / 1000:.0f} KB"


def _render(
    headers: list[str],
    records: list[dict],
    fields: list[str] | None = None,
    footer: str = "",
) -> str:
    """Header lines, then the rows as CSV, then an optional footer.

    Every tabular response goes through here — data, previews, summaries and
    entity matches — so they cannot drift into four dialects of the same thing.
    """
    parts = list(headers)
    if _has_negative_value(records, fields):
        parts.append(_SUPPRESSION_NOTE)
    parts += ["", records_to_csv(records, fields)]
    if footer:
        parts += ["", footer]
    return "\n".join(parts)


def format_too_large(
    what: str,
    total_count: int,
    estimated_chars: int,
    narrowing_hint: str,
    download_urls: list[tuple[str, str, str]] | None = None,
) -> str:
    """Refuse an over-budget query, actionably.

    A partial result is indistinguishable from a complete one once it reaches
    the caller, so an over-large query returns nothing but an explanation. The
    explanation has to carry everything needed to succeed next time — the true
    size, what would shrink it, and where the complete data lives — otherwise
    the caller just retries blindly.
    """
    lines = [
        "RESULT TOO LARGE — nothing was returned, so nothing here is partial.",
        "",
        f"{what} matches {total_count:,} rows ({_size(estimated_chars)}), over "
        f"the {_size(MAX_RESPONSE_CHARS)} response limit.",
        "",
        "No rows are shown deliberately: a truncated slice would be an arbitrary "
        "subset ordered by ID, which looks like an answer but is not a valid "
        "sample for counting, ranking, or averaging.",
        "",
        "To get an answer, do one of these:",
        f"  1. Narrow the query — {narrowing_hint}",
        "  2. Request fewer columns with fields=",
        "  3. Use get_summary to aggregate server-side instead of fetching rows",
        "  4. Use preview=True to see a labelled sample of what this table holds",
    ]
    downloads = _download_note(download_urls or [])
    if downloads:
        lines.append("  5. Download the complete dataset:")
        lines.extend(f"  {line}" for line in downloads)
    return "\n".join(lines)


def format_preview(
    records: list[dict],
    total_count: int,
    fields: list[str] | None = None,
) -> str:
    """Render a deliberately partial sample, labelled so it cannot pass as a result.

    This is the one place partial rows are allowed out, so the labelling carries
    the whole safety margin. The warning is repeated after the rows as well as
    before them: a long CSV block pushes a single header far up the context, and
    the risk being guarded against is precisely that these rows get treated as an
    answer. They are the API's first N by ID — not a random sample — so any
    count, ranking or average over them is wrong.
    """
    if not records:
        return "No records found matching your query."
    banner = (
        f"PREVIEW — NOT A RESULT SET. These are the first {len(records)} of "
        f"{total_count:,} matching rows in the API's own order (by ID), not a "
        f"random sample."
    )
    warning = (
        "Do NOT count, rank, average, or generalise from these rows — they are "
        "here only to show the shape of the data and typical values. For an "
        "answer, re-run without preview=True, narrowing or using fields= so the "
        "complete set fits."
    )
    return _render([banner, warning], records, fields, footer=f"↑ {banner} {warning}")


def format_ranked(
    records: list[dict],
    total_count: int,
    ordering: str,
    fields: list[str] | None = None,
    footer: str = "",
) -> str:
    """Render the leading rows of a SERVER-RANKED result.

    Unlike a preview this is an answer, not a sample. `ordering=` sorts in the
    API before it pages, so the first N rows really are the N highest or lowest —
    complete-or-refuse exists to stop an arbitrary ID-ordered prefix passing as
    an answer, and ranking is exactly what makes a prefix meaningful. Refusing it
    would withhold the one query shape that can answer "which are the biggest"
    over a table too large to return whole.

    The rows are still a subset, so the header says what they do and do not
    support: valid for ranking, invalid for totals.
    """
    if not records:
        return "No records found matching your query."
    field = ordering.lstrip("-")
    descending = ordering.startswith("-")
    direction = "highest" if descending else "lowest"
    banner = (
        f"Top {len(records)} of {total_count:,} matching rows, ranked by the API "
        f"on {field} ({direction} first)."
    )
    # Sentinel codes (-1 Missing, -2 Not applicable, -3 Suppressed) sort BELOW
    # every real value, so an ascending rank returns them first and the ranking
    # answers nothing. Only the ranked column matters here — negatives elsewhere
    # are the general suppression note's business, not this one's.
    if not descending and _has_negative_value(records, [field]):
        warning = (
            f"RANKING NOT MEANINGFUL — the lowest values of {field} here are "
            f"negative, which in this data are codes (-1 Missing, -2 Not "
            f"applicable, -3 Suppressed), not quantities. These rows are the "
            f"bottom of the sort, but they are not the smallest real values. "
            f"Filter the codes out (or rank descending) before drawing any "
            f"conclusion."
        )
    else:
        warning = (
            f"These are genuinely the {direction} {len(records)} by {field} — "
            f"valid for ranking questions. They are NOT the whole result set, so "
            f"do not total, average, or count over them."
        )
    return _render([banner, warning], records, fields, footer)


def format_data(
    records: list[dict],
    total_count: int,
    footer: str = "",
    fields: list[str] | None = None,
) -> str:
    """Format a COMPLETE set of data records as CSV.

    Callers establish that `records` holds every matching row before formatting,
    so there is no truncation branch here — a result that did not fit is
    returned as format_too_large instead.
    """
    if not records:
        return "No records found matching your query."
    return _render(
        [f"Returned {len(records):,} record(s) — complete result set."],
        records, fields, footer,
    )


def format_entity_matches(
    matches: list[dict],
    entity_type: str,
    name_field: str,
    id_field: str,
    columns: list[str] | None = None,
    note: str = "",
) -> str:
    """Format entity candidates as CSV.

    Same shape as every other table this server returns, rather than a bespoke
    indented layout: the caller has to read an ID and a city out of it either
    way, and one format is easier to reason about than two.

    `columns` is what the caller fetched, so a type carrying an extra
    disambiguating column (schools have school_level) shows it. Columns absent
    from every match are dropped rather than rendered as a blank field.

    `note` carries any confidence signal — a partial scan, a result cap, or a
    disambiguation prompt — so a guess can be told from a confident hit.
    """
    if not matches:
        return f"No {entity_type}s found matching your search."
    requested = columns or (id_field, name_field, "city_location", "fips")
    columns = [c for c in requested if any(c in m for m in matches)]
    return _render(
        [f"Found {len(matches)} matching {entity_type}(s):"],
        matches, columns, footer=f"Note: {note}" if note else "",
    )


def format_summary(
    results: list[dict],
    var: str,
    stat: str,
    by: str,
    filters: dict | None = None,
    footer: str = "",
) -> str:
    """Format a COMPLETE set of aggregation results as CSV.

    Rows carry the API's own column names rather than reconstructed
    ``field=value`` prose: the aggregate lands in a column named for the
    variable, so echoing the response shape avoids guessing which key holds the
    statistic. Numbers are written unmodified: size-conditional comma formatting
    (``if value > 1000``) drops the decimals from large averages while leaving
    smaller ones at full precision, so the same column reads at two precisions.
    """
    if not results:
        return "No summary results found."
    grouping = ", ".join([f.strip() for f in by.split(",") if f.strip()] + ["year"])
    filter_desc = (
        f" (filtered by {', '.join(f'{k}={v}' for k, v in filters.items())})"
        if filters else ""
    )
    return _render(
        [
            f"Summary: {stat.upper()}({var}) grouped by {grouping}{filter_desc}",
            f"Returned {len(results):,} row(s) — complete result set.",
        ],
        results, None, footer,
    )


def format_values(values: list[dict], format_name: str, codes: list[int] | None = None) -> str:
    """Format code-to-label mappings as readable text."""
    if codes is not None:
        values = [v for v in values if v.get("code") in codes]

    if not values:
        return f"No values found for format '{format_name}'."

    lines = [f"Values for format '{format_name}':\n"]
    for val in values:
        code = val.get("code", "?")
        label = strip_code_prefix(code, val.get("code_label", ""))
        lines.append(f"  {code} = {label}")

    return "\n".join(lines)
