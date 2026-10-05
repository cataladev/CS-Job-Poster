from __future__ import annotations

from job_bot.company_sources.base import CompanySource
from job_bot.company_sources.companies.ibm import IBMSource
from job_bot.company_sources.companies.microsoft import MicrosoftSource
from job_bot.company_sources.companies.nvidia import NvidiaSource
from job_bot.utils.http import AsyncHttpClient

COMPANY_SOURCE_REGISTRY: dict[str, type[CompanySource]] = {
    "ibm": IBMSource,
    "microsoft": MicrosoftSource,
    "nvidia": NvidiaSource,
}


def create_company_source(
    source_key: str, http_client: AsyncHttpClient
) -> CompanySource:
    """Construct one registered first-party company source."""

    try:
        source_class = COMPANY_SOURCE_REGISTRY[source_key]
    except KeyError as exc:
        raise ValueError(f"Unknown company source: {source_key}") from exc
    return source_class(http_client)
