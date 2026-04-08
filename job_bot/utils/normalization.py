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
    value = re.sub(r"[^a-z0-9\s,]+", " ", value)
    value = collapse_whitespace(value.replace(",", " "))
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
    job.season_term = job.season_term or detect_season_term(
        job.title,
        job.summary,
        job.internship_season or "",
        json.dumps(job.raw_data, sort_keys=True),
        job.canonical_url,
    )
    job.internship_season = job.internship_season or job.season_term
    job.summary = summarize_text(job.summary)
    job.dedupe_fingerprint = build_fingerprint(job)
    return job

