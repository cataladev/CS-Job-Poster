from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import asdict

from job_bot.company_sources.companies.ibm import IBMSource
from job_bot.github_feeds import JobObservation
from job_bot.utils.http import AsyncHttpClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Call IBM's live careers API and print adapted jobs without using the "
            "database or Discord."
        )
    )
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        help="number of adapted jobs to print (default: 10, maximum: 60)",
    )
    return parser.parse_args()


def validate(observations: list[JobObservation]) -> None:
    if len(observations) > IBMSource.result_limit:
        raise RuntimeError("IBM source returned more than its 60-result limit")
    for observation in observations:
        if observation.company != IBMSource.company_name:
            raise RuntimeError(f"unexpected company: {observation.company}")
        if observation.track not in {"internship", "new_grad"}:
            raise RuntimeError(f"unexpected track: {observation.track}")
        if observation.cycle is not None:
            raise RuntimeError(f"unexpected cycle: {observation.cycle}")
        if not observation.external_id:
            raise RuntimeError("IBM observation is missing external_id")
        if not observation.locations:
            raise RuntimeError(
                f"IBM observation {observation.external_id} is missing locations"
            )


def display_record(observation: JobObservation) -> dict[str, object]:
    record = asdict(observation)
    record.pop("raw_data", None)
    return record


async def run(show: int) -> None:
    async with AsyncHttpClient(
        timeout_seconds=30,
        retries=2,
        user_agent="CS-Job-Poster IBM smoke test/1.0",
    ) as http_client:
        result = await IBMSource(http_client).fetch()

    validate(result.observations)
    track_counts = Counter(job.track for job in result.observations)
    print(
        json.dumps(
            {
                "source_id": result.source_id,
                "requests_made": result.pages_checked,
                "adapted": len(result.observations),
                "rejected": result.rejected,
                "tracks": dict(sorted(track_counts.items())),
                "jobs": [
                    display_record(job)
                    for job in result.observations[: max(0, min(show, 60))]
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def main() -> None:
    args = parse_args()
    if args.show < 0:
        raise SystemExit("--show must not be negative")
    asyncio.run(run(args.show))


if __name__ == "__main__":
    main()
