from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from job_bot.models import JobPosting
from job_bot.utils.ats import extract_external_ids
from job_bot.utils.html import collapse_whitespace, strip_html


COMPANY_SUFFIXES = {
    "inc",
    "inc.",
    "llc",
    "l.l.c.",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "plc",
}
# These are verified employer-brand variants, not fuzzy matching. They are used
# only by direct-link enrichment so a Jobright label such as "NVIDIA AI" can
# safely reach the same employer board as "NVIDIA".
DIRECT_LINK_COMPANY_ALIASES = {
    "anduril industries": "anduril",
    "astranis space technologies": "astranis",
    "cadence": "cadence design systems",
    "tiktok": "bytedance",
    "tiktok usds joint venture": "bytedance",
    "creatify ai": "creatify lab",
    "exa": "exa labs",
    "amazon web services aws": "amazon",
    "magna international": "magna",
    "nvidia ai": "nvidia",
    "palantir technologies": "palantir",
    "sony electronics": "sony",
}
TITLE_SYNONYMS = {
    "software engineering": "software engineer",
    "software developer": "software engineer",
    "swe": "software engineer",
    "full stack": "fullstack",
    "full-stack": "fullstack",
    "front end": "frontend",
    "back end": "backend",
    "co-op": "coop",
    "co op": "coop",
    "internship": "intern",
}
LOCATION_ALIASES = {
    "sf": "san francisco ca",
    "san francisco": "san francisco ca",
    "nyc": "new york ny",
    "remote in usa": "remote us",
    "remote usa": "remote us",
    "remote-us": "remote us",
    "united states": "us",
    "u.s.": "us",
}
US_STATE_NAME_TO_CODE = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
    "district of columbia": "dc",
}
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"ref", "referrer", "source", "gh_src", "gh_source"}
SEASON_RE = re.compile(
    r"\b(summer|fall|winter|spring)\s+(20\d{2})\b",
    re.IGNORECASE,
)
YEAR_TERM_RE = re.compile(r"\b(20\d{2})\b")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    filtered = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in TRACKING_QUERY_KEYS:
            continue
        if any(lowered.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        filtered.append((key, value))
    path = re.sub(r"/+", "/", parsed.path or "/")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path.rstrip("/") or "/",
            "",
            urlencode(filtered),
            "",
        )
    )


def normalize_company_name(company: str) -> str:
    company = collapse_whitespace(company).lower()
    company = re.sub(r"[^a-z0-9\s&]+", " ", company)
    tokens = [token for token in company.split() if token not in COMPANY_SUFFIXES]
    return " ".join(tokens)


def direct_link_company_key(company: str) -> str:
    """Return a conservative employer key for direct-link cache lookups."""
    normalized = normalize_company_name(company)
    return DIRECT_LINK_COMPANY_ALIASES.get(normalized, normalized)


def normalize_title(title: str) -> str:
    title = collapse_whitespace(title).lower()
    title = title.replace("–", "-").replace("—", "-")
    for source, target in TITLE_SYNONYMS.items():
        title = title.replace(source, target)
    title = re.sub(r"[^a-z0-9\s/+]+", " ", title)
    tokens = [token for token in title.split() if token not in {"the", "and"}]
    return " ".join(tokens)


def normalize_location(location: str) -> str:
    value = collapse_whitespace(location).lower()
    value = value.replace("–", "-").replace("—", "-")
    if value in LOCATION_ALIASES:
        return LOCATION_ALIASES[value]
    # ApplyGuy and a few employer APIs emit locations country-first, e.g.
    # ``United States, Washington, Redmond``. Convert that representation to
    # the same city/state identity used by repository rows such as
    # ``Redmond, WA`` so one requisition cannot become two Discord posts.
    country_first = [part.strip() for part in value.split(",")]
    if (
        len(country_first) >= 3
        and country_first[0] in {"united states", "usa", "us", "u.s."}
        and country_first[1] in US_STATE_NAME_TO_CODE
    ):
        city = " ".join(country_first[2:]).strip()
        if city:
            return f"{city} {US_STATE_NAME_TO_CODE[country_first[1]]}"
    value = re.sub(r"[^a-z0-9\s,]+", " ", value)
    value = collapse_whitespace(value.replace(",", " "))
    # Greenhouse and similar boards commonly format a U.S. city as
    # ``Seattle, Washington, United States`` while Jobright emits
    # ``Seattle, WA``. Drop the redundant country before canonicalizing the
    # state so equivalent locations can share one direct link.
    value = re.sub(r"\b(?:united states|usa|us)\s*$", "", value).strip()
    for state_name, state_code in US_STATE_NAME_TO_CODE.items():
        state_suffixes = (f"{state_name} state", state_name)
        for suffix in state_suffixes:
            if value == suffix:
                return state_code
            if value.endswith(f" {suffix}"):
                value = f"{value[: -len(suffix)].strip()} {state_code}"
                break
    return LOCATION_ALIASES.get(value, value)


def collapse_location_cluster(locations: Iterable[str]) -> str:
    normalized = sorted({normalize_location(location) for location in locations if normalize_location(location)})
    if not normalized:
        return "unknown"
    if any(location.startswith("remote") for location in normalized):
        return "remote"
    if len(normalized) == 1:
        return normalized[0]
    return "multi"


def detect_remote_status(locations: Iterable[str]) -> str:
    joined = " ".join(location.lower() for location in locations)
    if "remote" in joined:
        return "remote"
    if "hybrid" in joined:
        return "hybrid"
    if joined.strip():
        return "onsite"
    return "unknown"


def detect_season_term(*values: str) -> Optional[str]:
    for value in values:
        match = SEASON_RE.search(value or "")
        if match:
            season = match.group(1).lower()
            year = match.group(2)
            return f"{season} {year}"
    for value in values:
        lowered = (value or "").lower()
        if "summer" in lowered:
            year = YEAR_TERM_RE.search(lowered)
            return f"summer {year.group(1)}" if year else "summer"
        if "fall" in lowered:
            year = YEAR_TERM_RE.search(lowered)
            return f"fall {year.group(1)}" if year else "fall"
        if "winter" in lowered:
            year = YEAR_TERM_RE.search(lowered)
            return f"winter {year.group(1)}" if year else "winter"
        if "spring" in lowered:
            year = YEAR_TERM_RE.search(lowered)
            return f"spring {year.group(1)}" if year else "spring"
    return None


def normalize_season_term(
    season_term: str | None,
    *,
    title: str,
    summary: str,
    internship_season: str,
    canonical_url: str,
) -> Optional[str]:
    """Prefer a specific season in the role text over a bare source-provided year."""
    existing = collapse_whitespace(season_term or "").casefold()
    detected = detect_season_term(title, summary, internship_season, canonical_url)
    if re.fullmatch(r"20\d{2}", existing) and detected:
        return detected
    return existing or detected


def build_fingerprint(job: JobPosting) -> str:
    payload = {
        "track": job.track,
        "company": job.normalized_company,
        "title": job.normalized_title,
        "season": job.season_term or "",
        "location": job.canonical_location,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def build_row_hash(parts: Iterable[str]) -> str:
    text = "||".join(collapse_whitespace(part) for part in parts if part is not None)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_text(value: str, limit: int = 280) -> str:
    cleaned = strip_html(value)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def normalize_job_posting(job: JobPosting) -> JobPosting:
    job.canonical_url = canonicalize_url(job.canonical_url or job.url)
    job.alternate_urls = sorted(
        {
            canonicalize_url(item)
            for item in ([job.url] + job.alternate_urls)
            if item
        }
    )
    job.external_ids = {
        **job.external_ids,
        **extract_external_ids(job.canonical_url, job.source_job_id),
    }
    job.normalized_company = normalize_company_name(job.company)
    job.normalized_title = normalize_title(job.title)
    job.normalized_locations = [
        normalize_location(location) for location in job.locations if normalize_location(location)
    ]
    job.canonical_location = collapse_location_cluster(job.locations)
    job.remote_status = detect_remote_status(job.locations)
    job.season_term = normalize_season_term(
        job.season_term,
        title=job.title,
        summary=job.summary,
        internship_season=job.internship_season or "",
        canonical_url=job.canonical_url,
    )
    job.internship_season = job.internship_season or job.season_term
    job.summary = summarize_text(job.summary)
    job.dedupe_fingerprint = build_fingerprint(job)
    return job
