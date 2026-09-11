"""Live-API smoke test: exercises the real endpoints, not the respx mocks.

Checks the paths that have only ever run against fixtures — summary filter
validation, the new column validation, and ranked queries end to end.
"""
import asyncio

from edp_mcp.server import (
    describe_dataset,
    get_data,
    get_summary,
    resolve_entity,
    search_datasets,
)

FAIL = []


def check(name, result, expect_ok=True, must_contain=None):
    bad_markers = ("Cannot filter on", "Unknown column", "Could not",
                   "No dataset matches", "is not available", "RESULT TOO LARGE")
    looks_bad = any(m in result[:400] for m in bad_markers)
    ok = (not looks_bad) if expect_ok else looks_bad
    if must_contain and must_contain not in result:
        ok = False
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        FAIL.append(name)
        print("   ->", " ".join(result[:300].split()))


async def main():
    r = await search_datasets(level="schools", source="ccd")
    check("search_datasets(schools, ccd)", r, must_contain="ccd/enrollment")

    r = await describe_dataset(path="schools/ccd/enrollment/{year}/{grade}/race/")
    check("describe_dataset(enrollment by race)", r, must_contain="[FILTER]")

    # Summary filter validation against the REAL varlist family.
    r = await get_summary(path="schools/ccd/enrollment", var="enrollment",
                          stat="sum", by="fips", filters="year=2022&grade=9")
    check("get_summary(+year,+grade filters)", r, must_contain="Summary:")

    r = await get_summary(path="schools/ccd/enrollment", var="enrollment",
                          stat="sum", by="fips", filters="charterr=1")
    check("get_summary rejects typo'd filter", r, expect_ok=False)

    r = await get_summary(path="schools/ccd/enrollment", var="enrollment",
                          stat="sum", by="fips", filters="year=1850")
    check("get_summary rejects impossible year", r, expect_ok=False)

    # A ranked query that FITS returns the complete set, still in ranked order.
    r = await get_data(path="schools/ccd/directory/2022/",
                       filters="fips=11&ordering=-enrollment",
                       fields="ncessch,school_name,enrollment", add_labels=False)
    check("ranked query that fits -> complete set", r,
          must_contain="complete result set")
    enrollments = [int(ln.split(",")[-1]) for ln in r.splitlines()
                   if ln.count(",") == 2 and ln.split(",")[-1].lstrip("-").isdigit()]
    ordered = enrollments == sorted(enrollments, reverse=True)
    print(f"[{'PASS' if ordered else 'FAIL'}] ranked rows actually descending "
          f"({enrollments[:4]}…)")
    if not ordered:
        FAIL.append("ranked order")

    # A ranked query too large to return whole takes the top-N path.
    r = await get_data(path="schools/ccd/directory/2022/",
                       filters="fips=6&ordering=-enrollment",
                       fields="ncessch,school_name,enrollment", add_labels=False)
    check("ranked query too large -> top-N", r, must_contain="ranked by the API")

    r = await get_data(path="schools/ccd/directory/2022/",
                       filters="fips=11&ordering=-enrolment")
    check("get_data rejects typo'd ordering", r, expect_ok=False)

    # Real columns must NOT be rejected by the new validation.
    r = await get_data(path="schools/ccd/directory/2022/", filters="fips=11",
                       fields="ncessch,school_name,enrollment,charter,fips",
                       add_labels=False)
    check("get_data accepts real fields", r, must_contain="complete result set")

    r = await resolve_entity(name="Dunbar", entity_type="school", fips=11)
    check("resolve_entity(school, DC)", r)

    print("\n" + ("ALL PASSED" if not FAIL else f"FAILURES: {FAIL}"))


asyncio.run(main())
