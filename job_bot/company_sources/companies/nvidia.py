from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit

from job_bot.company_sources.base import CompanySourceResult
from job_bot.github_feeds import JobObservation
from job_bot.utils.feed_normalization import (
    clean_feed_text,
    is_us_location,
    sanitize_application_url,
)
from job_bot.utils.http import AsyncHttpClient
from job_bot.watchlist import TrackClassifier


class NvidiaResponseError(ValueError):
    """Raised when NVIDIA returns an incomplete or unexpected search payload."""


class NvidiaSource:
    """Collect a bounded newest-first window from NVIDIA's public careers API."""

    source_id = "company_api_nvidia"
    company_name = "NVIDIA"
    source_url = "https://jobs.nvidia.com/careers"
    api_url = "https://jobs.nvidia.com/api/pcsx/search"
    max_pages = 20
    result_limit = 200
    job_types: ClassVar[tuple[str, ...]] = (
        "intern (fixed term)",
        "new college graduate",
        "academic (fixed term)",
        "regular employee (fixed term)",
    )
    headers: ClassVar[dict[str, str]] = {"Accept": "application/json"}

    def __init__(self, http_client: AsyncHttpClient) -> None:
        self.http_client = http_client

    @classmethod
    def build_params(cls, *, start: int = 0) -> list[tuple[str, str]]:
        # The UI's comma-separated filter becomes repeated keys in the API.
        return [
            ("domain", "nvidia.com"),
            ("query", ""),
            ("location", ""),
            ("start", str(start)),
            ("sort_by", "timestamp"),
            *(("filter_job_type", job_type) for job_type in cls.job_types),
        ]

    async def fetch(self) -> CompanySourceResult:
        observations: list[JobObservation] = []
        rejected = 0
        pages: list[object] = []
        seen_ids: set[str] = set()
        start = 0
        previous_date: str | None = None
        # The runner owns SCHEDULE_TIMEZONE. Retain yesterday UTC as well so
        # pagination cannot miss today's calendar date in any supported zone.
        oldest_day = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

        for _ in range(self.max_pages):
            data, _, _ = await self.http_client.get_json(
                self.api_url, params=self.build_params(start=start), headers=self.headers
            )
            positions, total = self._read_page(data)
            pages.append(data)
            if not positions and start < total:
                raise NvidiaResponseError("NVIDIA returned an empty page before its count")

            page_dates = [self._posted_date(position.get("postedTs")) for position in positions]
            for posted_date in page_dates:
                if posted_date is not None:
                    if previous_date is not None and posted_date > previous_date:
                        raise NvidiaResponseError("NVIDIA positions are not sorted newest first")
                    previous_date = posted_date

            new_ids = {
                clean_feed_text(position.get("id")) for position in positions
                if clean_feed_text(position.get("id"))
            } - seen_ids
            if positions and not new_ids:
                raise NvidiaResponseError("NVIDIA pagination did not advance")

            for position in positions[: self.result_limit - start]:
                position_id = clean_feed_text(position.get("id"))
                if position_id and position_id in seen_ids:
                    continue
                if position_id:
                    seen_ids.add(position_id)
                observation = self.to_observation(position)
                if observation is None:
                    rejected += 1
                else:
                    observations.append(observation)

            # Use the actual returned length: NVIDIA currently returns 10/page.
            start += len(positions)
            if start >= total or start >= self.result_limit or not positions:
                break
            if page_dates and all(page_dates) and page_dates[-1] < oldest_day:
                break

        content = json.dumps(pages, sort_keys=True, separators=(",", ":"), default=str)
        return CompanySourceResult(
            source_id=self.source_id,
            observations=observations,
            pages_checked=len(pages),
            rejected=rejected,
            content_sha=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def _read_page(cls, data: object) -> tuple[list[Mapping[str, Any]], int]:
        if not isinstance(data, Mapping) or data.get("status") != 200:
            raise NvidiaResponseError("NVIDIA response must be a successful JSON object")
        # Successful responses include an error object with empty message/body.
        error = data.get("error")
        if error and (not isinstance(error, Mapping) or any(error.values())):
            raise NvidiaResponseError("NVIDIA response contains an API error")
        inner = data.get("data")
        if not isinstance(inner, Mapping):
            raise NvidiaResponseError("NVIDIA response is missing the data object")
        positions = inner.get("positions")
        if not isinstance(positions, list) or not all(isinstance(p, Mapping) for p in positions):
            raise NvidiaResponseError("NVIDIA positions must be a list of objects")
        count = inner.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise NvidiaResponseError("NVIDIA response is missing a nonnegative numeric count")
        if len(positions) > count:
            raise NvidiaResponseError("NVIDIA positions exceed its count")
        if inner.get("sortBy") != "timestamp":
            raise NvidiaResponseError("NVIDIA response must confirm timestamp sorting")
        filters = inner.get("appliedFilters")
        job_types = filters.get("jobType") if isinstance(filters, Mapping) else None
        if (
            not isinstance(job_types, list)
            or not all(isinstance(value, str) for value in job_types)
            or set(job_types) != set(cls.job_types)
        ):
            raise NvidiaResponseError("NVIDIA response must confirm the requested job types")
        return positions, count

    def to_observation(self, position: Mapping[str, Any]) -> JobObservation | None:
        title = clean_feed_text(position.get("name"))
        external_id = clean_feed_text(
            position.get("atsJobId") or position.get("displayJobId") or position.get("id")
        )
        raw_url = position.get("positionUrl")
        if not isinstance(raw_url, str) or not raw_url:
            return None
        application_url = sanitize_application_url(urljoin(self.source_url, raw_url)).url
        if not application_url or urlsplit(application_url).hostname != "jobs.nvidia.com":
            return None
        if not re.fullmatch(r"/careers/job/\d+", urlsplit(application_url).path):
            return None

        locations = self._us_locations(position)
        track = self._track(title, clean_feed_text(position.get("department")))
        posted_date = self._posted_date(position.get("postedTs"))
        if not title or not external_id or not locations or not track or not posted_date:
            return None
        return JobObservation(
            source_id=self.source_id,
            source_label="NVIDIA Careers",
            source_url=self.source_url,
            source_ref=f"nvidia/{external_id}",
            external_id=external_id,
            company=self.company_name,
            title=title,
            locations=locations,
            application_url=application_url,
            track=track,
            posted_date=posted_date,
            cycle=None,
            raw_data=dict(position),
        )

    @staticmethod
    def _track(title: str, department: str) -> str | None:
        if re.search(r"\b(senior|sr\.?|staff|principal|director|manager|lead|recruiter)\b", title, re.I):
            return None
        normalized_title = re.sub(r"\bnew college grad(?:uate)?s?\b", "new grad", title, flags=re.I)
        return TrackClassifier().classify(
            title=normalized_title,
            employment_type="internship" if department.casefold() == "intern" else "",
        ).track

    @staticmethod
    def _us_locations(position: Mapping[str, Any]) -> list[str]:
        for field in ("locations", "standardizedLocations"):
            values = position.get(field)
            if not isinstance(values, list):
                continue
            locations: list[str] = []
            for value in values:
                if not isinstance(value, str):
                    continue
                location = clean_feed_text(value)
                parts = [part.strip() for part in location.split(",")]
                if field == "locations" and len(parts) >= 3 and parts[0].casefold() == "us":
                    location = ", ".join([*parts[2:], parts[1], "US"])
                elif field == "standardizedLocations":
                    # Two-letter foreign country codes can resemble U.S. states.
                    if not parts or parts[-1].casefold() != "us":
                        continue
                if location and is_us_location(location, trusted_us=False):
                    locations.append(location)
            if locations:
                return list(dict.fromkeys(locations))
        # Remote/hybrid describes work arrangements, not a country.
        return []

    @staticmethod
    def _posted_date(value: object) -> str | None:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value, tz=UTC).date().isoformat()
        except (OSError, ValueError, OverflowError):
            return None
