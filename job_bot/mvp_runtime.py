from __future__ import annotations

import asyncio
import logging
import time

import discord

from job_bot.config import configure_logging, load_config
from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline
from job_bot.feed_publisher import SingleChannelPublisher
from job_bot.models import AppConfig


class FeedDiscordClient(discord.Client):
    def __init__(self, *, config: AppConfig, pipeline: FeedPipeline) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = False
        super().__init__(intents=intents)
        self.config = config
        self.pipeline = pipeline
        self.logger = logging.getLogger(self.__class__.__name__)
        self._poll_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    async def on_ready(self) -> None:
        self.logger.info(
            "posting_runtime_ready user=%s channel=%s poll_interval_seconds=%s",
            self.user,
            self.config.discord_channel_id,
            self.config.poll_interval_seconds,
        )
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="new U.S. jobs",
            ),
        )
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop(), name="github-feed-poller")

    async def close(self) -> None:
        self._shutdown.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await super().close()

    async def _poll_loop(self) -> None:
        while not self.is_closed() and not self._shutdown.is_set():
            started = time.monotonic()
            try:
                await self.pipeline.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("feed_poll_failed")
            elapsed = time.monotonic() - started
            delay = max(1.0, self.config.poll_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue


async def run_bot() -> None:
    config = load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger("mvp_runtime")
    if not config.discord_token:
        raise RuntimeError("DISCORD_TOKEN is required")
    if not config.discord_channel_id:
        raise RuntimeError("DISCORD_CHANNEL_ID is required")
    if not config.database_url:
        raise RuntimeError("DATABASE_URL is required for the production posting runtime")
    if not config.database_url.casefold().startswith(("postgres://", "postgresql://")):
        raise RuntimeError("DATABASE_URL must use Postgres for duplicate-safe production delivery")

    database = Database(config.database_url)
    await database.initialize()
    if not await database.acquire_primary_runtime_lock():
        logger.critical("Another posting runtime holds the database lock; refusing to start")
        await database.close()
        return

    publisher = SingleChannelPublisher(config, database)
    client: FeedDiscordClient
    pipeline = FeedPipeline(config=config, database=database, publisher=publisher)
    client = FeedDiscordClient(config=config, pipeline=pipeline)
    publisher.bind_client(client)
    try:
        await client.start(config.discord_token)
    finally:
        if not client.is_closed():
            await client.close()
        await database.close()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
