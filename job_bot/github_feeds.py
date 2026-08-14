from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import markdown
from bs4 import BeautifulSoup, Tag

from job_bot.models import JobPosting
from job_bot.utils.feed_normalization import (
    clean_feed_text,
    is_us_location,
    sanitize_application_url,
)
from job_bot.utils.http import AsyncHttpClient
from job_bot.utils.normalization import build_row_hash, detect_season_term


@dataclass(frozen=True)
class JobObservation:
    source_id: str
    source_label: str
    source_url: str
    external_id: str
    company: str
    title: str
    locations: list[str]
    application_url: str
    track: str
    cycle: str | None = None
    posted_date: str | None = None
    source_ref: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_job_posting(self) -> JobPosting:
        summary_parts = [
            f"Cycle: {self.cycle}" if self.cycle else "",
            f"Posted: {self.posted_date}" if self.posted_date else "",
        ]
        return JobPosting(
            title=self.title,
            company=self.company,
            locations=self.locations or ["United States"],
            source_name=self.source_id,
            source_job_id=self.external_id,
            track=self.track,
            url=self.application_url,
            canonical_url=self.application_url,
            summary=". ".join(part for part in summary_parts if part),
            posted_date=self.posted_date,
            internship_season=self.cycle,
            season_term=self.cycle,
            raw_data={
                **self.raw_data,
                "source_label": self.source_label,
                "source_url": self.source_url,
                "source_ref": self.source_ref,
            },
            row_hash=build_row_hash([self.source_id, self.external_id]),
        )


Parser = Callable[[str, "FeedDefinition"], tuple[list[JobObservation], int]]


@dataclass(frozen=True)
class FeedDefinition:
    source_id: str
    source_label: str
    repository: str
    branch: str
    path: str
    track: str
    parser: Parser
    trusted_us: bool = False
    cycle_hint: str | None = None
    fetch_url: str | None = None

    @property
    def source_url(self) -> str:
        if self.fetch_url:
            return self.fetch_url
        return f"https://github.com/{self.repository}/blob/{self.branch}/{self.path}"

    @property
    def raw_url(self) -> str:
        if self.fetch_url:
            return self.fetch_url
        return f"https://raw.githubusercontent.com/{self.repository}/{self.branch}/{self.path}"


@dataclass(frozen=True)
class FeedFetchResult:
    definition: FeedDefinition
    observations: list[JobObservation]
    rejected: int
    etag: str | None
    last_modified: str | None
    content_sha: str | None
    not_modified: bool = False


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _epoch_date(value: object) -> str | None:
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _iso_date(value: object) -> str | None:
    match = re.match(r"(20\d{2}-\d{2}-\d{2})", str(value or "").strip())
    return match.group(1) if match else None


def _safe_url(raw_url: object) -> str:
    decision = sanitize_application_url(str(raw_url or ""))
    return decision.url


def _preferred_application_urls(raw_urls: Sequence[str]) -> list[str]:
    safe_urls = [url for raw in raw_urls if (url := _safe_url(raw))]
    if not safe_urls:
        return []
    aggregator_hosts = {"simplify.jobs", "www.simplify.jobs", "speedyapply.com"}
    direct_urls = [
        url
        for url in safe_urls
        if (urlsplit(url).hostname or "").casefold() not in aggregator_hosts
    ]
    preferred = direct_urls or [safe_urls[-1]]
    return list(dict.fromkeys(preferred))


def _preferred_application_url(raw_urls: Sequence[str]) -> str:
    """Compatibility helper for callers that only need one destination."""
    urls = _preferred_application_urls(raw_urls)
    return urls[0] if urls else ""


def _us_locations(
    locations: Sequence[object],
    *,
    trusted_us: bool,
    context: str,
) -> list[str]:
    cleaned: list[str] = []
    for location in locations:
        for fragment in re.split(r"\s*(?:\||;|\r?\n)\s*", str(location or "")):
            value = clean_feed_text(fragment)
            if value:
                cleaned.append(value)
    if not cleaned:
        return ["United States"] if trusted_us else []
    accepted = [
        location
        for location in cleaned
        if is_us_location(location, trusted_us=trusted_us, context=context)
    ]
    if accepted:
        return accepted
    return []


def _feed_locations(
    locations: Sequence[object],
    *,
    definition: FeedDefinition,
    context: str,
) -> list[str]:
    return _us_locations(
        locations,
        trusted_us=definition.trusted_us,
        context=context,
    )


def _location_cell_values(cell: Tag) -> list[str]:
    values = [clean_feed_text(value) for value in cell.stripped_strings]
    values = [value for value in values if value]
    if len(values) > 1 and re.fullmatch(r"\d+\s+locations?", values[0], re.IGNORECASE):
        values = values[1:]
    return values


def _company_name(value: str, previous: str | None) -> tuple[str, str | None]:
    if value.strip() in {"", "↳"}:
        return previous or "", previous
    cleaned = re.sub(r"^[🔥⭐\s]+", "", value).strip()
    cleaned = re.sub(r"\s*[✓🆁]+$", "", cleaned).strip()
    return cleaned, cleaned or previous


def parse_simplify_json(text: str, definition: FeedDefinition) -> tuple[list[JobObservation], int]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{definition.source_id} returned invalid JSON") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{definition.source_id} must return a non-empty JSON array")

    observations: list[JobObservation] = []
    rejected = 0
    for item in payload:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        if not item.get("active") or item.get("is_visible") is False:
            continue
        company = clean_feed_text(item.get("company_name"))
        title = clean_feed_text(item.get("title"))
        application_url = _safe_url(item.get("url"))
        locations = _feed_locations(
            _sequence(item.get("locations")),
            definition=definition,
            context=f"{title} {application_url}",
        )
        if not company or not title or not application_url or not locations:
            rejected += 1
            continue
        terms = [clean_feed_text(term) for term in _sequence(item.get("terms")) if term]
        cycle = detect_season_term(*terms, title) if definition.track == "internship" else None
        if definition.track == "new_grad":
            year = re.search(r"\b20(?:26|27|28|29)\b", " ".join([title, *terms]))
            cycle = year.group(0) if year else definition.cycle_hint
        external_id = clean_feed_text(item.get("id")) or application_url
        observations.append(
            JobObservation(
                source_id=definition.source_id,
                source_label=definition.source_label,
                source_url=definition.source_url,
                source_ref=f"{definition.branch}/{definition.path}",
                external_id=external_id,
                company=company,
                title=title,
                locations=locations,
                application_url=application_url,
                track=definition.track,
                cycle=cycle,
                posted_date=_epoch_date(item.get("date_posted")),
                raw_data={"terms": terms, "sponsorship": item.get("sponsorship")},
            )
        )
    if not observations:
        raise ValueError(f"{definition.source_id} produced no active U.S. observations")
    return observations, rejected


def _header_map(table: Tag) -> tuple[list[str], dict[str, int]]:
    header_cells = table.find_all("th")
    headers = [clean_feed_text(cell.get_text(" ", strip=True)).casefold() for cell in header_cells]
    return headers, {header: index for index, header in enumerate(headers)}


def _column_index(indexes: Mapping[str, int], *names: str) -> int | None:
    for name in names:
        if name in indexes:
            return indexes[name]
    return None


def _age_date(value: str) -> str | None:
    iso = _iso_date(value)
    if iso:
        return iso
    match = re.search(r"\b(\d+)\s*d\b", value, flags=re.IGNORECASE)
    if match:
        return (datetime.now(UTC).date() - timedelta(days=int(match.group(1)))).isoformat()
    return None


def parse_markdown_table(text: str, definition: FeedDefinition) -> tuple[list[JobObservation], int]:
    if not text.strip():
        raise ValueError(f"{definition.source_id} returned empty Markdown")
    soup = BeautifulSoup(markdown.markdown(text, extensions=["tables"]), "html.parser")
    observations: list[JobObservation] = []
    rejected = 0

    for table in soup.find_all("table"):
        _headers, indexes = _header_map(table)
        company_index = _column_index(indexes, "company")
        title_index = _column_index(indexes, "position", "role", "title")
        location_index = _column_index(indexes, "location", "locations")
        apply_index = _column_index(indexes, "posting", "apply", "application")
        date_index = _column_index(indexes, "added", "age", "date", "posted")
        term_index = _column_index(indexes, "term", "terms", "season", "cycle")
        if None in {company_index, title_index, location_index, apply_index}:
            continue

        previous_company: str | None = None
        for row in table.find_all("tr")[1:]:
            if "🔒" in row.get_text(" ", strip=True):
                continue
            cells = row.find_all("td")
            required_max = max(company_index, title_index, location_index, apply_index)
            if len(cells) <= required_max:
                rejected += 1
                continue
            raw_company = clean_feed_text(cells[company_index].get_text(" ", strip=True))
            company, previous_company = _company_name(raw_company, previous_company)
            title = clean_feed_text(cells[title_index].get_text(" ", strip=True))
            raw_locations = _location_cell_values(cells[location_index])
            link_cells = cells[location_index + 1 :]
            links = [
                str(link.get("href") or "")
                for cell in link_cells
                for link in cell.find_all("a")
            ]
            application_urls = _preferred_application_urls(links)
            locations = _feed_locations(
                raw_locations,
                definition=definition,
                context=f"{title} {' '.join(application_urls)}",
            )
            if not company or not title or not application_urls or not locations:
                rejected += 1
                continue

            raw_date = ""
            if date_index is not None and date_index < len(cells):
                raw_date = clean_feed_text(cells[date_index].get_text(" ", strip=True))
            if not _age_date(raw_date):
                for cell in reversed(cells[location_index + 1 :]):
                    candidate = clean_feed_text(cell.get_text(" ", strip=True))
                    if _age_date(candidate):
                        raw_date = candidate
                        break

            term = ""
            if term_index is not None and term_index < len(cells):
                term = clean_feed_text(cells[term_index].get_text(" ", strip=True))
            if not term:
                term = " ".join(
                    clean_feed_text(cell.get_text(" ", strip=True))
                    for cell in cells[location_index + 1 :]
                    if not cell.find("a")
                )
            cycle = definition.cycle_hint
            if definition.track == "internship":
                cycle = (
                    detect_season_term(title, term, definition.cycle_hint or "")
                    or definition.cycle_hint
                )
            elif definition.track == "new_grad":
                year = re.search(r"\b20(?:26|27|28|29)\b", title)
                cycle = year.group(0) if year else definition.cycle_hint
            for application_url in application_urls:
                observations.append(
                    JobObservation(
                        source_id=definition.source_id,
                        source_label=definition.source_label,
                        source_url=definition.source_url,
                        source_ref=f"{definition.branch}/{definition.path}",
                        external_id=application_url,
                        company=company,
                        title=title,
                        locations=locations,
                        application_url=application_url,
                        track=definition.track,
                        cycle=cycle,
                        posted_date=_age_date(raw_date),
                        raw_data={"source_date": raw_date, "term": term},
                    )
                )
    if not observations:
        raise ValueError(f"{definition.source_id} produced no active U.S. observations")
    return observations, rejected


def parse_intern_engine_json(text: str, definition: FeedDefinition) -> tuple[list[JobObservation], int]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{definition.source_id} returned invalid JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list) or not payload["jobs"]:
        raise ValueError(f"{definition.source_id} must return an object with a non-empty jobs array")

    observations: list[JobObservation] = []
    rejected = 0
    for item in payload["jobs"]:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        company = clean_feed_text(item.get("company"))
        title = clean_feed_text(item.get("title"))
        application_url = _safe_url(item.get("url"))
        location = clean_feed_text(item.get("location"))
        locations = _feed_locations(
            [location],
            definition=definition,
            context=f"{title} {application_url}",
        )
        if not company or not title or not application_url or not locations:
            rejected += 1
            continue
        program = clean_feed_text(item.get("program")).casefold()
        track = "new_grad" if "grad" in program else "internship"
        observations.append(
            JobObservation(
                source_id=definition.source_id,
                source_label=definition.source_label,
                source_url=definition.source_url,
                source_ref=f"{definition.branch}/{definition.path}",
                external_id=clean_feed_text(item.get("id")) or application_url,
                company=company,
                title=title,
                locations=locations,
                application_url=application_url,
                track=track,
                cycle=clean_feed_text(item.get("season")) or None,
                posted_date=_iso_date(item.get("posted_at")),
                raw_data={"category": item.get("category"), "source": item.get("source")},
            )
        )
    if not observations:
        raise ValueError(f"{definition.source_id} produced no active U.S. observations")
    return observations, rejected


def parse_jobright_next_data(text: str, definition: FeedDefinition) -> tuple[list[JobObservation], int]:
    """Parse Jobright's public, server-rendered U.S. role listings.

    These listing pages expose the displayed job cards in Next.js' ``__NEXT_DATA__``
    payload. The personalized ``/jobs/recommend`` view is deliberately not used:
    it depends on a user's account preferences and is not a stable shared feed.
    """
    soup = BeautifulSoup(text, "html.parser")
    data_node = soup.find("script", id="__NEXT_DATA__")
    if data_node is None or not data_node.string:
        raise ValueError(f"{definition.source_id} is missing its Next.js payload")
    try:
        payload = json.loads(data_node.string)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{definition.source_id} returned invalid Next.js JSON") from exc

    if not isinstance(payload, Mapping):
        raise ValueError(f"{definition.source_id} returned an invalid Next.js payload")
    props = payload.get("props")
    page_props = props.get("pageProps") if isinstance(props, Mapping) else None
    job_list = page_props.get("jobList") if isinstance(page_props, Mapping) else None
    if not isinstance(job_list, list) or not job_list:
        raise ValueError(f"{definition.source_id} produced no public job cards")

    observations: list[JobObservation] = []
    rejected = 0
    for item in job_list:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        job = item.get("jobResult")
        company_data = item.get("companyResult")
        if not isinstance(job, Mapping) or not isinstance(company_data, Mapping):
            rejected += 1
            continue

        title = clean_feed_text(job.get("jobTitle"))
        company = clean_feed_text(company_data.get("companyName"))
        application_url = _safe_url(job.get("applyLink") or job.get("url"))
        locations = _feed_locations(
            _sequence(job.get("jobLocations")) or (job.get("jobLocation"),),
            definition=definition,
            context=f"{title} {application_url}",
        )
        seniority = clean_feed_text(job.get("jobSeniority")).casefold()
        employment_type = clean_feed_text(job.get("employmentType")).casefold()
        is_expected_track = (
            "intern" in seniority or "intern" in employment_type
            if definition.track == "internship"
            else "new grad" in seniority
        )
        if not title or not company or not application_url or not locations or not is_expected_track:
            rejected += 1
            continue

        cycle: str | None = None
        if definition.track == "internship":
            cycle = detect_season_term(
                title,
                clean_feed_text(job.get("internHireDate")),
            )
        else:
            year = re.search(r"\b20(?:26|27|28|29)\b", title)
            cycle = year.group(0) if year else definition.cycle_hint

        external_id = clean_feed_text(job.get("jobId")) or application_url
        observations.append(
            JobObservation(
                source_id=definition.source_id,
                source_label=definition.source_label,
                source_url=definition.source_url,
                source_ref=f"{definition.branch}/{definition.path}",
                external_id=external_id,
                company=company,
                title=title,
                locations=locations,
                application_url=application_url,
                track=definition.track,
                cycle=cycle,
                posted_date=_iso_date(job.get("publishTime")),
                raw_data={
                    "jobright_seniority": seniority,
                    "employment_type": employment_type,
                    "work_model": clean_feed_text(job.get("workModel")),
                    "salary": clean_feed_text(job.get("salaryDesc")),
                },
            )
        )

    if not observations:
        raise ValueError(f"{definition.source_id} produced no active U.S. observations")
    return observations, rejected


FEEDS: tuple[FeedDefinition, ...] = (
    FeedDefinition(
        "simplify_internships",
        "Simplify",
        "SimplifyJobs/Summer2027-Internships",
        "dev",
        "README.md",
        "internship",
        parse_markdown_table,
        cycle_hint="Summer 2027",
    ),
    FeedDefinition(
        "simplify_new_grad",
        "Simplify",
        "SimplifyJobs/New-Grad-Positions",
        "dev",
        "README.md",
        "new_grad",
        parse_markdown_table,
        cycle_hint="2027",
    ),
    FeedDefinition(
        "simplify_offseason_internships",
        "Simplify",
        "SimplifyJobs/Summer2027-Internships",
        "dev",
        "README-Off-Season.md",
        "internship",
        parse_markdown_table,
    ),
    FeedDefinition(
        "speedy_internships",
        "SpeedyApply",
        "speedyapply/2027-SWE-College-Jobs",
        "main",
        "README.md",
        "internship",
        parse_markdown_table,
    ),
    FeedDefinition(
        "speedy_new_grad",
        "SpeedyApply",
        "speedyapply/2027-SWE-College-Jobs",
        "main",
        "NEW_GRAD_USA.md",
        "new_grad",
        parse_markdown_table,
        cycle_hint="2027",
    ),
    FeedDefinition(
        "sndsh_internships",
        "Summer 2027 list",
        "sndsh404/summer-2027-internships",
        "main",
        "README.md",
        "internship",
        parse_markdown_table,
        cycle_hint="Summer 2027",
    ),
    FeedDefinition(
        "intern_engine",
        "Internship Engine",
        "zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships",
        "main",
        "docs/api/jobs.json",
        "internship",
        parse_intern_engine_json,
    ),
    FeedDefinition(
        "intern_engine_readme",
        "Internship Engine",
        "zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships",
        "main",
        "README.md",
        "internship",
        parse_markdown_table,
    ),
    FeedDefinition(
        "jobright_internships",
        "Jobright",
        "jobright.ai",
        "public",
        "software-engineer-intern-jobs-in-united-states",
        "internship",
        parse_jobright_next_data,
        fetch_url="https://jobright.ai/jobs/software-engineer-intern-jobs-in-united-states",
    ),
    FeedDefinition(
        "jobright_new_grad",
        "Jobright",
        "jobright.ai",
        "public",
        "software-engineer-new-graduate-jobs-in-united-states",
        "new_grad",
        parse_jobright_next_data,
        fetch_url="https://jobright.ai/jobs/software-engineer-new-graduate-jobs-in-united-states",
    ),
)

FEED_BY_ID = {feed.source_id: feed for feed in FEEDS}


class GitHubFeedFetcher:
    async def fetch(
        self,
        definition: FeedDefinition,
        http_client: AsyncHttpClient,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FeedFetchResult:
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        text, response_headers, status = await http_client.get_text(
            definition.raw_url,
            headers=headers,
        )
        if status == 304:
            return FeedFetchResult(
                definition=definition,
                observations=[],
                rejected=0,
                etag=etag,
                last_modified=last_modified,
                content_sha=None,
                not_modified=True,
            )
        observations, rejected = definition.parser(text, definition)
        return FeedFetchResult(
            definition=definition,
            observations=observations,
            rejected=rejected,
            etag=response_headers.get("ETag"),
            last_modified=response_headers.get("Last-Modified"),
            content_sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
