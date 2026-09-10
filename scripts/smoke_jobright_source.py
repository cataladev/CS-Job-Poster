from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import asdict

from dotenv import load_dotenv

from job_bot.github_feeds import (
    FEED_BY_ID,
    JOBRIGHT_PUBLIC_WINDOW_SIZE,
    JOBRIGHT_TAXONOMY_IDS,
    GitHubFeedFetcher,
    JobObservation,
)
from job_bot.utils.http import AsyncHttpClient

JOBRIGHT_SOURCE_IDS = ("jobright_internships", "jobright_new_grad")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Call Jobright's anonymous visitor window and print adapted jobs without "
            "using the database or Discord."
        )
    )
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        help="number of adapted jobs to print per track (default: 10)",
    )
    return parser.parse_args()


def validate(observations: list[JobObservation], *, track: str) -> None:
    for observation in observations:
        if observation.track != track:
            raise RuntimeError(
                f"{observation.external_id} has track {observation.track}, expected {track}"
            )
        if not observation.external_id:
            raise RuntimeError("Jobright observation is missing external_id")
        if not observation.company:
            raise RuntimeError(
                f"Jobright observation {observation.external_id} is missing a company"
            )
        if not observation.locations:
            raise RuntimeError(
                f"Jobright observation {observation.external_id} is missing locations"
            )
        if not observation.application_url.startswith("https://"):
            raise RuntimeError(
                f"Jobright observation {observation.external_id} has an unsafe link"
            )


def display_record(observation: JobObservation) -> dict[str, object]:
    record = asdict(observation)
    record.pop("raw_data", None)
    return record


async def run(show: int) -> None:
    # Picks up JOBRIGHT_PROXY_* from .env the way the runtime's load_config() does.
    load_dotenv()
    # One fetcher instance so both tracks share the window, exactly as the runtime does.
    fetcher = GitHubFeedFetcher()
    report: dict[str, object] = {
        "taxonomies_requested": len(JOBRIGHT_TAXONOMY_IDS),
        "window_size": JOBRIGHT_PUBLIC_WINDOW_SIZE,
        "tracks": {},
    }
    # Jobright's own requests leave through the fetcher's Chrome-impersonating
    # client; this one only satisfies the shared signature every other feed uses.
    async with AsyncHttpClient(
        timeout_seconds=30,
        retries=2,
        user_agent="CS-Job-Poster Jobright smoke test/1.0",
    ) as http_client:
        for source_id in JOBRIGHT_SOURCE_IDS:
            definition = FEED_BY_ID[source_id]
            result = await fetcher.fetch(definition, http_client)
            validate(result.observations, track=definition.track)
            companies = Counter(job.company for job in result.observations)
            report["tracks"][source_id] = {  # type: ignore[index]
                "degraded_reason": result.degraded_reason,
                "adapted": len(result.observations),
                "rejected": result.rejected,
                "distinct_companies": len(companies),
                "jobs": [
                    display_record(job) for job in result.observations[: max(0, show)]
                ],
            }

    # Read after the fetches, so a rotation away from a refused address is visible.
    report["egress"] = fetcher.jobright_egress_label()
    report["seconds_until_retry"] = int(fetcher.jobright_seconds_until_retry())
    print(json.dumps(report, indent=2, sort_keys=True))


def main() -> None:
    args = parse_args()
    if args.show < 0:
        raise SystemExit("--show must not be negative")
    asyncio.run(run(args.show))


if __name__ == "__main__":
    main()
