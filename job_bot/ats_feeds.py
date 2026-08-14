from __future__ import annotations

import csv
import hashlib
import json
import re
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


DIRECT_ATS_PROVIDERS = ("greenhouse", "lever", "ashby", "smartrecruiters")


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
        parser=parse_direct_ats_json,
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
    return None


def _observation_from_item(
    item: Mapping[str, Any],
    definition: FeedDefinition,
    classifier: TrackClassifier,
) -> JobObservation | None:
    provider = definition.provider
    title = _text(item.get("title") or item.get("text") or item.get("name"))
    summary = _text(
        item.get("content")
        or item.get("descriptionPlain")
        or item.get("description")
        or item.get("jobAd"))
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
    external_id = _text(item.get("id") or item.get("uuid") or item.get("refNumber"))
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
    return ()


def _posted_date(item: Mapping[str, Any], provider: str | None) -> str | None:
    if provider == "lever":
        return _epoch_date(item.get("createdAt"))
    return _iso_date(
        item.get("updated_at")
        or item.get("publishedAt")
        or item.get("releasedDate")
        or item.get("createdAt")
    )


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
