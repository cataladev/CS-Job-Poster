from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlparse

from job_bot.company_sources.base import CompanySourceResult
from job_bot.github_feeds import JobObservation
from job_bot.utils.feed_normalization import (
    clean_feed_text,
    is_us_location,
    sanitize_application_url,
)
from job_bot.utils.http import AsyncHttpClient


class IBMResponseError(ValueError):
    """Raised when IBM returns an incomplete or unexpected search payload."""


class IBMSource:
    """Fetch selected early-career U.S. roles from IBM's careers API."""

    source_id = "company_api_ibm"
    company_name = "IBM"
    source_url = "https://www.ibm.com/careers"
    api_url = "https://www-api.ibm.com/search/api/v2"
    result_limit = 60
    career_areas: ClassVar[tuple[str, ...]] = (
        "Software Engineering",
        "Infrastructure & Technology",
        "Data & Analytics",
        "Cloud",
    )
    career_area_keys: ClassVar[frozenset[str]] = frozenset(
        area.casefold() for area in career_areas
    )
    headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.ibm.com",
        "Referer": "https://www.ibm.com/",
    }
    source_fields = (
        "_id",
        "title",
        "url",
        "description",
        "language",
        "entitled",
        "field_keyword_05",
        "field_keyword_08",
        "field_keyword_17",
        "field_keyword_18",
        "field_keyword_19",
        "dcdate",
    )

    def __init__(self, http_client: AsyncHttpClient) -> None:
        self.http_client = http_client

    @classmethod
    def build_payload(cls) -> dict[str, Any]:
        return {
            "appId": "careers",
            "scopes": ["careers2"],
            "query": {
                "bool": {
                    "must": [
                        {
                            "terms": {
                                "field_keyword_08": list(cls.career_areas)
                            }
                        },
                        {
                            "terms": {
                                "field_keyword_18": ["Internship", "Entry Level"]
                            }
                        },
                    ]
                }
            },
            "size": cls.result_limit,
            "from": 0,
            "sort": [{"dcdate": "desc"}, {"_score": "desc"}],
            "lang": "zz",
            "localeSelector": {},
            "sm": {"query": "", "lang": "zz"},
            "_source": list(cls.source_fields),
        }

    async def fetch(self) -> CompanySourceResult:
        observations: list[JobObservation] = []
        rejected = 0
        data, _, _ = await self.http_client.post_json(
            self.api_url,
            headers=self.headers,
            json=self.build_payload(),
        )
        raw_jobs, _ = self._read_page(data)

        for raw_job in raw_jobs[: self.result_limit]:
            observation = self.to_observation(raw_job)
            if observation is None:
                rejected += 1
            else:
                observations.append(observation)

        content = json.dumps(
            data, sort_keys=True, separators=(",", ":"), default=str
        )
        return CompanySourceResult(
            source_id=self.source_id,
            observations=observations,
            pages_checked=1,
            rejected=rejected,
            content_sha=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def _read_page(cls, data: object) -> tuple[list[Mapping[str, Any]], int]:
        if not isinstance(data, Mapping):
            raise IBMResponseError("IBM response must be a JSON object")
        hits_container = data.get("hits")
        if not isinstance(hits_container, Mapping):
            raise IBMResponseError("IBM response is missing the hits object")
        raw_hits = hits_container.get("hits")
        if not isinstance(raw_hits, list) or not all(
            isinstance(hit, Mapping) for hit in raw_hits
        ):
            raise IBMResponseError("IBM response hits must be a list of objects")
        total = cls._read_total(hits_container)
        return raw_hits, total

    @staticmethod
    def _read_total(hits_container: Mapping[str, Any]) -> int:
        total_container = hits_container.get("total")
        if isinstance(total_container, int) and not isinstance(total_container, bool):
            return total_container
        if isinstance(total_container, Mapping):
            value = total_container.get("value")
            relation = total_container.get("relation", "eq")
            if relation != "eq":
                raise IBMResponseError("IBM response hit total must be exact")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        raise IBMResponseError("IBM response is missing a numeric hit total")

    def to_observation(self, raw_job: Mapping[str, Any]) -> JobObservation | None:
        source = raw_job.get("_source")
        if not isinstance(source, Mapping):
            return None

        title = clean_feed_text(source.get("title"))
        raw_application_url = str(source.get("url") or "")
        external_id = self._source_id(raw_application_url, raw_job)
        application_url = sanitize_application_url(raw_application_url).url
        country = clean_feed_text(source.get("field_keyword_05"))
        career_area = clean_feed_text(source.get("field_keyword_08"))
        raw_location = clean_feed_text(source.get("field_keyword_19"))
        location = self._us_location(raw_location, country, application_url)
        experience = clean_feed_text(source.get("field_keyword_18"))
        track = {
            "entry level": "new_grad",
            "internship": "internship",
        }.get(experience.casefold())
        if not title or not application_url or not external_id or not location:
            return None
        if career_area.casefold() not in self.career_area_keys or track is None:
            return None

        posted_date = self._posted_date(source.get("dcdate"))
        return JobObservation(
            source_id=self.source_id,
            source_label="IBM Careers",
            source_url=self.source_url,
            source_ref=f"ibm/{external_id}",
            external_id=external_id,
            company=self.company_name,
            title=title,
            locations=[location],
            application_url=application_url,
            track=track,
            cycle=None,
            posted_date=posted_date,
            raw_data=dict(raw_job),
        )

    @staticmethod
    def _source_id(application_url: str, raw_job: Mapping[str, Any]) -> str:
        for name, value in parse_qsl(urlparse(application_url).query):
            if name.casefold() == "jobid":
                job_id = clean_feed_text(value)
                if job_id:
                    return job_id
        return clean_feed_text(raw_job.get("_id"))

    @staticmethod
    def _us_location(location: str, country: str, application_url: str) -> str:
        country_key = country.casefold()
        country_is_us = country_key in {
            "us",
            "usa",
            "united states",
            "united states of america",
        }
        if not country_is_us:
            return ""
        if location and is_us_location(
            location,
            trusted_us=True,
            context=f"{country} {application_url}",
        ):
            return location
        return "United States"

    @staticmethod
    def _posted_date(value: object) -> str | None:
        text = clean_feed_text(value)
        if len(text) >= 10 and text[:4].isdigit() and text[4] == "-" and text[7] == "-":
            return text[:10]
        return None
