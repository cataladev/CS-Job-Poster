from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from job_bot.github_feeds import (
    FeedDefinition,
    JobObservation,
    _epoch_date,
    _feed_locations,
    _iso_date,
    _safe_url,
)
from job_bot.watchlist import TrackClassifier


DIRECT_ATS_PROVIDERS = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workday",
    "oracle",
    "optiver",
    "bytedance",
    "rippling",
    "amazon",
)

# Public boards confirmed against the providers' own anonymous APIs. These cover
# high-signal employer spellings that do not yet appear in the historical
# watchlist seed. They are still fetched only when a current Jobright role names
# the employer, then cooled down by the direct-link cache state.
CURATED_DIRECT_ATS_BOARDS = (
    ("Anduril", "greenhouse", "andurilindustries", "https://job-boards.greenhouse.io/andurilindustries"),
    ("Astranis", "greenhouse", "astranis", "https://job-boards.greenhouse.io/astranis"),
    ("Appian", "greenhouse", "appian", "https://job-boards.greenhouse.io/appian"),
    ("Figma", "greenhouse", "figma", "https://job-boards.greenhouse.io/figma"),
    ("Nuro", "greenhouse", "nuro", "https://job-boards.greenhouse.io/nuro"),
    ("Jump Trading", "greenhouse", "jumptrading", "https://job-boards.greenhouse.io/jumptrading"),
    ("IMC Trading", "greenhouse", "imc", "https://job-boards.greenhouse.io/imc"),
    ("Abridge", "ashby", "abridge", "https://jobs.ashbyhq.com/abridge"),
    ("Exa", "ashby", "exa", "https://jobs.ashbyhq.com/exa"),
    ("Neptune.io", "ashby", "neptune", "https://jobs.ashbyhq.com/neptune"),
    ("Creatify AI", "ashby", "creatify", "https://jobs.ashbyhq.com/creatify"),
    (
        "Optiver",
        "optiver",
        "https://www.optiver.com/join-us/jobs/?level=internship&numberposts=100",
        "https://www.optiver.com/join-us/jobs/?level=internship&numberposts=100",
    ),
    ("ByteDance", "bytedance", "en", "https://joinbytedance.com/search"),
    ("Rippling", "rippling", "rippling", "https://www.rippling.com/careers/open-roles"),
    ("Amazon", "amazon", "amazon", "https://www.amazon.jobs/en/"),
)


def build_direct_ats_feeds(
    seed_path: str,
    *,
    poll_interval_seconds: int,
) -> tuple[FeedDefinition, ...]:
    """Build one reviewed, direct-public-board feed per distinct ATS board.

    The seed already contains employer career URLs. We derive only known public ATS
    board identifiers from those URLs; arbitrary company pages are deliberately not
    fetched by this fast path.
    """
    path = Path(seed_path)
    if not path.exists():
        return ()

    targets: dict[tuple[str, str], tuple[tuple[int, str], str, str, str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("active", "true")).strip().casefold() not in {
                "1",
                "true",
                "yes",
                "on",
            }:
                continue
            careers_url = str(row.get("careers_url") or "").strip()
            company = str(row.get("company") or "").strip()
            target = _target_from_careers_url(careers_url)
            if not target or not company:
                continue
            provider, token = target
            # Prefer the highest-priority spelling when the historic seed has aliases.
            priority = (_tier_rank(str(row.get("tier") or "t3")), company.casefold())
            key = (provider, token.casefold())
            existing = targets.get(key)
            if existing is None or priority < existing[0]:
                targets[key] = (priority, token, company, careers_url)

    for company, provider, token, careers_url in CURATED_DIRECT_ATS_BOARDS:
        key = (provider, token.casefold())
        if key not in targets:
            targets[key] = ((1, company.casefold()), token, company, careers_url)

    feeds = [
            _definition_for_target(
                provider=provider,
                token=token,
            company_name=company_name,
            careers_url=careers_url,
            poll_interval_seconds=poll_interval_seconds,
        )
        for (provider, _), (_, token, company_name, careers_url) in targets.items()
    ]
    return tuple(sorted(feeds, key=lambda feed: feed.source_id))


def parse_direct_ats_json(
    text: str,
    definition: FeedDefinition,
) -> tuple[list[JobObservation], int]:
    if definition.provider not in DIRECT_ATS_PROVIDERS:
        raise ValueError(f"Unsupported direct ATS provider: {definition.provider}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{definition.source_id} returned invalid JSON") from exc

    items = _items_for_provider(payload, definition.provider)
    if items is None:
        raise ValueError(f"{definition.source_id} returned an invalid jobs payload")

    classifier = TrackClassifier()
    observations: list[JobObservation] = []
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        observation = _observation_from_item(item, definition, classifier)
        if observation is None:
            continue
        observations.append(observation)
    return observations, rejected


def parse_optiver_html(
    text: str,
    definition: FeedDefinition,
) -> tuple[list[JobObservation], int]:
    """Parse Optiver's server-rendered public internship cards."""
    soup = BeautifulSoup(text, "html.parser")
    classifier = TrackClassifier()
    observations: list[JobObservation] = []
    seen_urls: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if "/join-us/jobs/" not in href:
            continue
        title = _text(link.get_text(" ", strip=True))
        application_url = _safe_url(
            href if href.startswith("http") else f"https://www.optiver.com{href}"
        )
        if not title or not application_url or application_url in seen_urls:
            continue
        seen_urls.add(application_url)
        classification = classifier.classify(title=title, summary="", employment_type="")
        if classification.track is None:
            continue
        card = link.find_parent("div", class_=lambda classes: classes and "group" in classes)
        card_text = _text(card.get_text(" ", strip=True) if card else "")
        location = _optiver_location(card_text, title)
        locations = _feed_locations(
            (location,), definition=definition, context=f"{title} {application_url}"
        )
        if not locations:
            continue
        observations.append(
            JobObservation(
                source_id=definition.source_id,
                source_label=definition.source_label,
                source_url=definition.source_url,
                source_ref=f"optiver/{definition.path}",
                external_id=application_url,
                company=definition.company_name or "Optiver",
                title=title,
                locations=locations,
                application_url=application_url,
                track=classification.track,
                cycle=_cycle_for(title, classification.track),
            )
        )
    if not observations:
        raise ValueError(f"{definition.source_id} produced no Optiver early-career roles")
    return observations, 0


def parse_bytedance_json(
    text: str,
    definition: FeedDefinition,
) -> tuple[list[JobObservation], int]:
    """Normalize ByteDance's public careers-search response."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{definition.source_id} returned invalid ByteDance JSON") from exc
    data = payload.get("data") if isinstance(payload, Mapping) else None
    items = data.get("job_post_list") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        raise ValueError(f"{definition.source_id} returned invalid ByteDance jobs")
    classifier = TrackClassifier()
    observations: list[JobObservation] = []
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        title = _text(item.get("title"))
        summary = _text(item.get("description"))
        recruit_type = item.get("recruit_type")
        employment_type = _text(
            recruit_type.get("en_name") if isinstance(recruit_type, Mapping) else ""
        )
        classification = classifier.classify(
            title=title, summary=summary, employment_type=employment_type
        )
        direct_id = _text(item.get("id"))
        city = item.get("city_info")
        locations = _feed_locations(
            (_bytedance_location(city),),
            definition=definition,
            context=title,
        )
        if classification.track is None or not title or not direct_id or not locations:
            continue
        application_url = f"https://joinbytedance.com/search/{quote(direct_id, safe='')}"
        observations.append(
            JobObservation(
                source_id=definition.source_id,
                source_label=definition.source_label,
                source_url=definition.source_url,
                source_ref="bytedance/en",
                external_id=direct_id,
                company=definition.company_name or "ByteDance",
                title=title,
                locations=locations,
                application_url=application_url,
                track=classification.track,
                cycle=_cycle_for(title, classification.track),
            )
        )
    return observations, rejected


def parse_rippling_json(
    text: str,
    definition: FeedDefinition,
) -> tuple[list[JobObservation], int]:
    """Normalize Rippling's public careers-search results.

    The public index includes non-U.S. roles, so country validation happens from
    the structured location fields instead of inferring it from display text.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{definition.source_id} returned invalid Rippling JSON") from exc
    results = payload.get("results") if isinstance(payload, Mapping) else None
    first_result = results[0] if isinstance(results, list) and results else None
    items = first_result.get("hits") if isinstance(first_result, Mapping) else None
    if not isinstance(items, list):
        raise ValueError(f"{definition.source_id} returned invalid Rippling jobs")

    classifier = TrackClassifier()
    observations: list[JobObservation] = []
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        title = _text(item.get("name"))
        application_url = _safe_url(item.get("url"))
        direct_id = _text(item.get("jobId")) or _text(item.get("objectID"))
        structured_locations = item.get("locations")
        locations = _feed_locations(
            (
                location.get("name")
                for location in structured_locations
                if isinstance(location, Mapping)
                and _text(location.get("countryCode")).casefold() == "us"
            )
            if isinstance(structured_locations, list)
            else (),
            definition=definition,
            context=f"{title} {application_url}",
        )
        classification = classifier.classify(title=title, summary="", employment_type="")
        if classification.track is None or not title or not direct_id or not application_url or not locations:
            continue
        observations.append(
            JobObservation(
                source_id=definition.source_id,
                source_label=definition.source_label,
                source_url=definition.source_url,
                source_ref="rippling/careers_en-US_production",
                external_id=direct_id,
                company=definition.company_name or "Rippling",
                title=title,
                locations=locations,
                application_url=application_url,
                track=classification.track,
                cycle=_cycle_for(title, classification.track),
            )
        )
    return observations, rejected


def parse_amazon_json(
    text: str,
    definition: FeedDefinition,
) -> tuple[list[JobObservation], int]:
    """Normalize Amazon's public, unauthenticated careers-search response."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{definition.source_id} returned invalid Amazon JSON") from exc
    items = payload.get("jobs") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        raise ValueError(f"{definition.source_id} returned invalid Amazon jobs")
    classifier = TrackClassifier()
    observations: list[JobObservation] = []
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        title = _text(item.get("title"))
        path = _text(item.get("job_path"))
        application_url = _safe_url(
            path if path.startswith("http") else f"https://www.amazon.jobs{path}"
        )
        classification = classifier.classify(
            title=title,
            summary=_text(item.get("description")),
            employment_type="",
        )
        locations = _feed_locations(
            (_amazon_location(_text(item.get("location"))),),
            definition=definition,
            context=f"{title} {application_url}",
        )
        direct_id = _text(item.get("id")) or path
        if classification.track is None or not title or not direct_id or not application_url or not locations:
            continue
        observations.append(
            JobObservation(
                source_id=definition.source_id,
                source_label=definition.source_label,
                source_url=definition.source_url,
                source_ref="amazon/jobs-search",
                external_id=direct_id,
                company=definition.company_name or "Amazon",
                title=title,
                locations=locations,
                application_url=application_url,
                track=classification.track,
                cycle=_cycle_for(title, classification.track),
                posted_date=_amazon_posted_date(_text(item.get("posted_date"))),
            )
        )
    return observations, rejected


def _target_from_careers_url(careers_url: str) -> tuple[str, str] | None:
    parsed = urlparse(careers_url)
    host = parsed.netloc.casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if host.endswith("greenhouse.io") and (
        host.startswith("job-boards.") or host.startswith("boards.")
    ):
        return "greenhouse", parts[0]
    if host == "jobs.lever.co":
        return "lever", parts[0]
    if host == "jobs.ashbyhq.com":
        return "ashby", parts[0]
    if host == "jobs.smartrecruiters.com":
        return "smartrecruiters", parts[0]
    if "myworkdayjobs.com" in host:
        # Workday's anonymous CXS jobs endpoint needs all three pieces. Keep the
        # token structured so a board cannot accidentally be queried on a
        # different tenant or career site.
        site_index = 1 if len(parts) > 1 and re.fullmatch(
            r"[a-z]{2}(?:-[a-z]{2})?", parts[0], flags=re.IGNORECASE
        ) else 0
        site = parts[site_index]
        tenant = host.split(".", 1)[0]
        return "workday", json.dumps(
            {"host": host, "site": site, "tenant": tenant}, sort_keys=True
        )
    if host.endswith("oraclecloud.com"):
        try:
            site = parts[parts.index("sites") + 1]
        except (ValueError, IndexError):
            return None
        return "oracle", json.dumps({"host": host, "site": site}, sort_keys=True)
    if host == "www.optiver.com" and parsed.path.startswith("/join-us/jobs"):
        return "optiver", careers_url
    return None


def _definition_for_target(
    *,
    provider: str,
    token: str,
    company_name: str,
    careers_url: str,
    poll_interval_seconds: int,
) -> FeedDefinition:
    token_digest = hashlib.sha256(f"{provider}:{token.casefold()}".encode()).hexdigest()[:16]
    return FeedDefinition(
        source_id=f"direct_{provider}_{token_digest}",
        source_label=f"{company_name} direct careers",
        repository=f"direct-ats/{provider}",
        branch="public",
        path=token,
        track="direct_ats",
        parser=(
            parse_optiver_html
            if provider == "optiver"
            else parse_bytedance_json
            if provider == "bytedance"
            else parse_rippling_json
            if provider == "rippling"
            else parse_amazon_json
            if provider == "amazon"
            else parse_direct_ats_json
        ),
        fetch_url=_api_url(provider, token),
        provider=provider,
        company_name=company_name,
        poll_interval_seconds=poll_interval_seconds,
        required_for_baseline=False,
    )


def _api_url(provider: str, token: str) -> str:
    encoded = quote(token, safe="")
    if provider == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{encoded}/jobs?content=true"
    if provider == "lever":
        return f"https://api.lever.co/v0/postings/{encoded}?mode=json"
    if provider == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{encoded}"
    if provider == "smartrecruiters":
        return (
            "https://api.smartrecruiters.com/v1/companies/"
            f"{encoded}/postings?limit=100&offset=0"
        )
    if provider == "workday":
        board = _workday_board(token)
        return f"https://{board['host']}/wday/cxs/{board['tenant']}/{board['site']}/jobs"
    if provider == "oracle":
        board = _oracle_board(token)
        return (
            f"https://{board['host']}/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
            f"&finder=findReqs;siteNumber={quote(board['site'], safe='')},limit=20,offset=0"
        )
    if provider == "optiver":
        return token
    if provider == "bytedance":
        return "https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts"
    if provider == "rippling":
        return "https://6fnax3tbef-dsn.algolia.net/1/indexes/*/queries"
    if provider == "amazon":
        return "https://www.amazon.jobs/en/search.json"
    raise ValueError(f"Unsupported direct ATS provider: {provider}")


def _items_for_provider(payload: Any, provider: str) -> Sequence[Any] | None:
    if provider == "lever":
        return payload if isinstance(payload, list) else None
    if not isinstance(payload, Mapping):
        return None
    if provider in {"greenhouse", "ashby"}:
        jobs = payload.get("jobs")
        return jobs if isinstance(jobs, list) else None
    if provider == "smartrecruiters":
        items = payload.get("content")
        return items if isinstance(items, list) else None
    if provider == "workday":
        items = payload.get("jobPostings")
        return items if isinstance(items, list) else None
    if provider == "oracle":
        searches = payload.get("items")
        if not isinstance(searches, list) or not searches:
            return None
        first = searches[0]
        items = first.get("requisitionList") if isinstance(first, Mapping) else None
        return items if isinstance(items, list) else None
    return None


def _observation_from_item(
    item: Mapping[str, Any],
    definition: FeedDefinition,
    classifier: TrackClassifier,
) -> JobObservation | None:
    provider = definition.provider
    title = _text(item.get("title") or item.get("Title") or item.get("text") or item.get("name"))
    summary = _text(
        item.get("content")
        or item.get("descriptionPlain")
        or item.get("description")
        or item.get("jobAd")
        or item.get("ShortDescriptionStr")
    )
    type_of_employment = item.get("typeOfEmployment")
    employment_type = _text(
        item.get("employmentType")
        or (
            type_of_employment.get("label")
            if isinstance(type_of_employment, Mapping)
            else ""
        )
    )
    classification = classifier.classify(
        title=title,
        summary=summary,
        employment_type=employment_type,
    )
    if classification.track is None:
        return None

    application_url = _safe_url(_application_url(item, definition))
    locations = _feed_locations(
        _locations(item, provider),
        definition=definition,
        context=f"{title} {application_url}",
    )
    external_id = _text(
        item.get("id")
        or item.get("Id")
        or item.get("uuid")
        or item.get("refNumber")
        or item.get("externalPath")
    )
    if not title or not application_url or not locations or not external_id:
        return None
    cycle = _cycle_for(title, classification.track)
    return JobObservation(
        source_id=definition.source_id,
        source_label=definition.source_label,
        source_url=definition.source_url,
        source_ref=f"{provider}/{definition.path}",
        external_id=external_id,
        company=definition.company_name or definition.source_label,
        title=title,
        locations=locations,
        application_url=application_url,
        track=classification.track,
        cycle=cycle,
        posted_date=_posted_date(item, provider),
        raw_data={"provider": provider, "board_token": definition.path},
    )


def _application_url(item: Mapping[str, Any], definition: FeedDefinition) -> object:
    provider = definition.provider
    if provider == "greenhouse":
        return item.get("absolute_url")
    if provider == "lever":
        return item.get("hostedUrl") or item.get("applyUrl")
    if provider == "ashby":
        url = item.get("jobUrl") or item.get("applyUrl")
        return f"https://jobs.ashbyhq.com{url}" if isinstance(url, str) and url.startswith("/") else url
    if provider == "smartrecruiters":
        direct_id = item.get("id") or item.get("uuid")
        if direct_id:
            return f"https://jobs.smartrecruiters.com/{definition.path}/{direct_id}"
        return item.get("applyUrl") or item.get("jobAdUrl") or item.get("ref")
    if provider == "workday":
        external_path = _text(item.get("externalPath"))
        if not external_path:
            return ""
        board = _workday_board(definition.path)
        return (
            f"https://{board['host']}/{board['site']}{external_path}"
            if external_path.startswith("/")
            else external_path
        )
    if provider == "oracle":
        board = _oracle_board(definition.path)
        direct_id = _text(item.get("Id"))
        return (
            f"https://{board['host']}/hcmUI/CandidateExperience/en/sites/"
            f"{board['site']}/job/{quote(direct_id, safe='')}"
            if direct_id
            else ""
        )
    return ""


def _locations(item: Mapping[str, Any], provider: str | None) -> Iterable[object]:
    if provider == "greenhouse":
        location = item.get("location")
        return (location.get("name"),) if isinstance(location, Mapping) else (location,)
    if provider == "lever":
        categories = item.get("categories")
        return (categories.get("location"),) if isinstance(categories, Mapping) else ()
    if provider == "ashby":
        values: list[object] = [item.get("locationName") or item.get("location")]
        for secondary in item.get("secondaryLocations", []):
            if isinstance(secondary, Mapping):
                values.append(secondary.get("locationName") or secondary.get("location"))
        return values
    if provider == "smartrecruiters":
        location = item.get("location")
        if isinstance(location, Mapping):
            if location.get("fullLocation"):
                return (location["fullLocation"],)
            return (
                ", ".join(
                    filter(None, (_text(location.get("city")), _text(location.get("region")), _text(location.get("country"))))
                ),
            )
        return (location,)
    if provider == "workday":
        return (_workday_location(value) for value in _text(item.get("locationsText")).split("|"))
    if provider == "oracle":
        return (item.get("PrimaryLocation"),)
    return ()


def _posted_date(item: Mapping[str, Any], provider: str | None) -> str | None:
    if provider == "lever":
        return _epoch_date(item.get("createdAt"))
    if provider == "oracle":
        return _iso_date(item.get("PostedDate"))
    return _iso_date(
        item.get("updated_at")
        or item.get("publishedAt")
        or item.get("releasedDate")
        or item.get("createdAt")
    )


def _workday_board(token: str) -> dict[str, str]:
    try:
        payload = json.loads(token)
    except json.JSONDecodeError as exc:
        raise ValueError("Workday board token is invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Workday board token is invalid")
    board = {key: _text(payload.get(key)) for key in ("host", "tenant", "site")}
    if not all(board.values()):
        raise ValueError("Workday board token is incomplete")
    return board


def _oracle_board(token: str) -> dict[str, str]:
    try:
        payload = json.loads(token)
    except json.JSONDecodeError as exc:
        raise ValueError("Oracle board token is invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Oracle board token is invalid")
    board = {key: _text(payload.get(key)) for key in ("host", "site")}
    if not all(board.values()):
        raise ValueError("Oracle board token is incomplete")
    return board


def _workday_location(value: str) -> str:
    """Convert Workday's country-first labels to the shared city/state shape."""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) >= 3 and parts[0].casefold() in {"us", "usa", "united states"}:
        state = parts[1]
        if re.fullmatch(r"[A-Z]{2}", state, flags=re.IGNORECASE):
            return f"{', '.join(parts[2:])}, {state.upper()}"
    return value


def _optiver_location(card_text: str, title: str) -> str:
    remainder = card_text.removeprefix(title).strip()
    city = remainder.split(" ", 1)[0] if remainder else ""
    return {"Austin": "Austin, TX", "Chicago": "Chicago, IL"}.get(city, city)


def _bytedance_location(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    city = _text(value.get("en_name"))
    parent = value.get("parent")
    state = _text(parent.get("en_name")) if isinstance(parent, Mapping) else ""
    country = parent.get("parent") if isinstance(parent, Mapping) else None
    country_name = _text(country.get("en_name")) if isinstance(country, Mapping) else ""
    if country_name not in {"United States", "United States of America"}:
        return ""
    return ", ".join(part for part in (city, state) if part)


def _amazon_location(value: str) -> str:
    """Convert Amazon's `US, WA, Seattle` form to city/state."""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) >= 3 and parts[0].casefold() in {"us", "usa", "united states"}:
        return f"{parts[-1]}, {parts[-2].upper()}"
    return value


def _amazon_posted_date(value: str) -> str | None:
    for pattern in ("%B %d, %Y", "%B  %d, %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _cycle_for(title: str, track: str) -> str | None:
    year = re.search(r"\b20(?:26|27|28|29)\b", title)
    if track == "new_grad":
        return year.group(0) if year else None
    return year.group(0) if year else None


def _text(value: object) -> str:
    if not value:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").stripped_strings)


def _tier_rank(tier: str) -> int:
    return {"t1": 1, "t2": 2, "t3": 3}.get(tier.casefold(), 9)
