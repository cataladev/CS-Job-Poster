from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from job_bot.github_feeds import JobObservation
from job_bot.utils.http import AsyncHttpClient


@dataclass(frozen=True)
class CompanySourceResult:
    """One complete, validated snapshot from a first-party careers API."""

    source_id: str
    observations: list[JobObservation]
    pages_checked: int
    rejected: int = 0
    content_sha: str | None = None


class CompanySource(Protocol):
    """Contract implemented by every first-party company careers source."""

    source_id: str
    company_name: str
    source_url: str

    def __init__(self, http_client: AsyncHttpClient) -> None: ...

    async def fetch(self) -> CompanySourceResult:
        """Fetch and adapt one complete current source snapshot."""

        ...
