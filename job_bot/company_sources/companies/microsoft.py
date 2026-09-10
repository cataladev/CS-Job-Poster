from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from job_bot.company_sources.base import CompanySourceResult
from job_bot.github_feeds import JobObservation
from job_bot.utils.feed_normalization import (
    clean_feed_text,
    is_us_location,
    sanitize_application_url,
)
from job_bot.utils.http import AsyncHttpClient


class MicrosoftResponseError(ValueError):
    """Raised when Microsoft returns an incomplete or unexpected search payload."""


class MicrosoftSource:
    """Fetch intern and new-grad U.S. roles from Microsoft's careers API."""

    source_id = "company_api_microsoft"
    company_name = "Microsoft"
    source_url = "https://careers.microsoft.com"
    api_url = "https://apply.careers.microsoft.com/api/pcsx/search"
    page_size = 20
    max_pages = 10
    result_limit = 200
    seniority_filters: ClassVar[tuple[str, ...]] = ("Intern", "Recent Graduate")
    headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
    }

    def __init__(self, http_client: AsyncHttpClient) -> None:
        self.http_client = http_client

    @classmethod
    def build_params(cls, *, seniority: str, start: int = 0) -> dict[str, str]:
        return {
            "domain": "microsoft.com",
            "query": "",
            "location": "United States",
            "start": str(start),
            "filter_seniority": seniority,
            "sort_by": "timestamp",
        }

    async def fetch(self) -> CompanySourceResult:
        observations: list[JobObservation] = []
        rejected = 0
        pages_checked = 0
        all_raw: list[Any] = []

        for seniority in self.seniority_filters:
            track = {
                "intern": "internship",
                "recent graduate": "new_grad",
            }.get(seniority.casefold())
            if track is None:
                continue

            start = 0
            while start < self.result_limit:
                params = self.build_params(seniority=seniority, start=start)
                data, _, _ = await self.http_client.get_json(
                    self.api_url, params=params, headers=self.headers
                )
                positions, total = self._read_page(data)
                pages_checked += 1
                all_raw.append(data)

                for position in positions:
                    observation = self.to_observation(position, track=track)
                    if observation is None:
                        rejected += 1
                    else:
                        observations.append(observation)

                start += self.page_size
                if start >= total or not positions:
                    break

        content = json.dumps(
            all_raw, sort_keys=True, separators=(",", ":"), default=str
        )
        return CompanySourceResult(
            source_id=self.source_id,
            observations=observations,
            pages_checked=pages_checked,
            rejected=rejected,
            content_sha=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def _read_page(cls, data: object) -> tuple[list[Mapping[str, Any]], int]:
        if not isinstance(data, Mapping):
            raise MicrosoftResponseError("Microsoft response must be a JSON object")
        inner = data.get("data")
        if not isinstance(inner, Mapping):
            raise MicrosoftResponseError("Microsoft response is missing the data object")
        positions = inner.get("positions")
        if not isinstance(positions, list) or not all(
            isinstance(p, Mapping) for p in positions
        ):
            raise MicrosoftResponseError(
                "Microsoft response positions must be a list of objects"
            )
        count = inner.get("count")
        if not isinstance(count, int) or isinstance(count, bool):
            raise MicrosoftResponseError(
                "Microsoft response is missing a numeric count"
            )
        return positions, count

    def to_observation(
        self, position: Mapping[str, Any], *, track: str
    ) -> JobObservation | None:
        title = clean_feed_text(position.get("name"))
        external_id = clean_feed_text(position.get("displayJobId") or position.get("id"))
        position_url = str(position.get("positionUrl") or "")

        if position_url.startswith("/"):
            application_url = f"https://apply.careers.microsoft.com{position_url}"
        else:
            application_url = position_url

        sanitized = sanitize_application_url(application_url)
        application_url = sanitized.url

        if not title or not application_url or not external_id:
            return None

        location = self._extract_us_location(position)
        if not location:
            return None

        posted_date = self._posted_date(position.get("postedTs"))

        return JobObservation(
            source_id=self.source_id,
            source_label="Microsoft Careers",
            source_url=self.source_url,
            source_ref=f"microsoft/{external_id}",
            external_id=external_id,
            company=self.company_name,
            title=title,
            locations=[location],
            application_url=application_url,
            track=track,
            cycle=None,
            posted_date=posted_date,
            raw_data=dict(position),
        )

    @staticmethod
    def _extract_us_location(position: Mapping[str, Any]) -> str:
        standardized = position.get("standardizedLocations")
        if isinstance(standardized, list):
            for loc in standardized:
                loc_str = clean_feed_text(loc)
                if loc_str and is_us_location(
                    loc_str, trusted_us=False, context="microsoft"
                ):
                    return loc_str

        raw_locations = position.get("locations")
        if isinstance(raw_locations, list):
            for loc in raw_locations:
                loc_str = clean_feed_text(loc)
                if loc_str and is_us_location(
                    loc_str, trusted_us=False, context="microsoft"
                ):
                    return loc_str

        work_option = clean_feed_text(position.get("workLocationOption"))
        if work_option.casefold() == "remote":
            return "United States (Remote)"

        return ""

    @staticmethod
    def _posted_date(value: object) -> str | None:
        if isinstance(value, (int, float)):
            try:
                dt = datetime.fromtimestamp(value, tz=UTC)
                return dt.strftime("%Y-%m-%d")
            except (OSError, ValueError, OverflowError):
                return None
        text = clean_feed_text(value)
        if (
            len(text) >= 10
            and text[:4].isdigit()
            and text[4] == "-"
            and text[7] == "-"
        ):
            return text[:10]
        return None
