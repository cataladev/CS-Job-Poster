"""Stable, compact source identifiers for job footers; see SOURCE_CODES.md."""

from collections.abc import Iterable


# Assign codes explicitly so reordering feeds or rolling over repositories to a
# new recruiting year cannot change the meaning of an existing Discord footer.
SOURCE_CODES = {
    "simplify_internships": "GH-1",
    "simplify_new_grad": "GH-2",
    "simplify_offseason_internships": "GH-3",
    "speedy_internships": "GH-4",
    "speedy_new_grad": "GH-5",
    "sndsh_internships": "GH-6",
    "intern_engine": "GH-7",
    "intern_engine_readme": "GH-8",
    "applyguy_internships": "GH-9",
    "applyguy_new_grad": "GH-10",
    "jobright_internships": "JB-13",
    "jobright_new_grad": "JB-13",
}


def source_codes(source_ids: Iterable[str]) -> list[str]:
    """Return distinct known codes in a consistent, numeric order.

    Use ingestion source IDs, not application URLs: resolving a Jobright link to
    an employer's URL does not make that observation a direct-scraper discovery.
    Unknown IDs are omitted rather than exposing internal names in Discord.
    """
    codes: set[str] = set()
    for source_id in source_ids:
        code = SOURCE_CODES.get(source_id)
        if code:
            codes.add(code)
        elif source_id.startswith(("direct_", "company_api_")):
            codes.add("DS-1")
    return sorted(codes, key=lambda code: (code.split("-")[0], int(code.split("-")[1])))
