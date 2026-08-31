from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from job_bot.company_sources.runner import CompanySourceRunner
from job_bot.config import load_config
from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline
from job_bot.utils.http import AsyncHttpClient


class NoDiscordPublisher:
    async def publish_pending(self) -> tuple[int, int]:
        return 0, 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run IBM's live source through normalization, dedupe, and PostgreSQL "
            "without connecting to Discord."
        )
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=2,
        help="number of consecutive IBM runs (default: 2 to demonstrate dedupe)",
    )
    return parser.parse_args()


async def run(run_count: int) -> None:
    config = load_config()
    if not config.database_url.casefold().startswith(("postgres://", "postgresql://")):
        raise SystemExit("DATABASE_URL must point to the development PostgreSQL database")

    database = Database(config.database_url)
    await database.initialize()
    publisher = NoDiscordPublisher()
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=publisher,
        feeds=(),
        direct_link_feeds=(),
    )
    runner = CompanySourceRunner(
        config=config,
        database=database,
        pipeline=pipeline,
    )
    results = []
    try:
        async with AsyncHttpClient(
            timeout_seconds=config.request_timeout_seconds,
            retries=config.request_retries,
            user_agent=config.user_agent,
        ) as http_client:
            for _ in range(run_count):
                results.append(asdict(await runner.run("ibm", http_client)))

        ibm_jobs = await database.search_text("IBM", limit=1_000)
        print(
            json.dumps(
                {
                    "discord_connected": False,
                    "runs": results,
                    "stored_ibm_canonicals": len(ibm_jobs),
                    "delivery_states": await database.get_delivery_counts(),
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await database.close()


def main() -> None:
    args = parse_args()
    if args.runs < 1:
        raise SystemExit("--runs must be at least 1")
    asyncio.run(run(args.runs))


if __name__ == "__main__":
    main()
