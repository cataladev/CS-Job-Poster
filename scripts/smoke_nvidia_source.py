from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import asdict

from job_bot.company_sources.companies.nvidia import NvidiaSource
from job_bot.utils.http import AsyncHttpClient


async def run(show: int) -> None:
    async with AsyncHttpClient(
        timeout_seconds=30,
        retries=2,
        user_agent="CS-Job-Poster NVIDIA smoke test/1.0",
    ) as http_client:
        result = await NvidiaSource(http_client).fetch()

    jobs = []
    for observation in result.observations[:show]:
        record = asdict(observation)
        record.pop("raw_data", None)
        jobs.append(record)
    print(json.dumps({
        "source_id": result.source_id,
        "pages_checked": result.pages_checked,
        "adapted": len(result.observations),
        "rejected": result.rejected,
        "tracks": dict(Counter(job.track for job in result.observations)),
        "jobs": jobs,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe NVIDIA's live careers API without using the database or Discord."
    )
    parser.add_argument("--show", type=int, default=10, help="maximum jobs to print")
    args = parser.parse_args()
    if args.show < 0:
        parser.error("--show must be nonnegative")
    asyncio.run(run(args.show))


if __name__ == "__main__":
    main()
