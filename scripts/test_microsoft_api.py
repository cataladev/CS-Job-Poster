"""Quick smoke test: hit the real Microsoft careers API and print results."""

import asyncio
import sys

sys.path.insert(0, ".")

from job_bot.company_sources.companies.microsoft import MicrosoftSource
from job_bot.utils.http import AsyncHttpClient


async def main() -> None:
    async with AsyncHttpClient(
        timeout_seconds=30, retries=2, user_agent="CS-Job-Poster/test"
    ) as http:
        source = MicrosoftSource(http)
        result = await source.fetch()

    print(f"Source: {result.source_id}")
    print(f"Pages checked: {result.pages_checked}")
    print(f"Observations: {len(result.observations)}")
    print(f"Rejected: {result.rejected}")
    print()

    for i, obs in enumerate(result.observations[:10]):
        print(f"[{i+1}] {obs.title}")
        print(f"    Company:  {obs.company}")
        print(f"    Track:    {obs.track}")
        print(f"    Location: {', '.join(obs.locations)}")
        print(f"    Posted:   {obs.posted_date}")
        print(f"    URL:      {obs.application_url}")
        print()

    if len(result.observations) > 10:
        print(f"... and {len(result.observations) - 10} more")


if __name__ == "__main__":
    asyncio.run(main())
