from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands

from job_bot.config import apply_runtime_channel_overrides, configure_logging, load_config
from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline
from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.job_channel_commands import (
    JobChannelConfigurator,
    register_job_channel_commands,
)
from job_bot.models import AppConfig


class FeedDiscordClient(discord.Client):
    def __init__(
        self,
        *,
        config: AppConfig,
        database: Database,
        pipeline: FeedPipeline,
        publisher: TrackChannelPublisher,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = False
        super().__init__(intents=intents)
        self.config = config
        self.pipeline = pipeline
        self.logger = logging.getLogger(self.__class__.__name__)
        self.tree = app_commands.CommandTree(self)
        self.channel_configurator = JobChannelConfigurator(config, database)
        register_job_channel_commands(
            self.tree,
            configurator=self.channel_configurator,
            publisher=publisher,
        )
        self._poll_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()
        self._guild_commands_synced = False

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def on_ready(self) -> None:
        self.logger.info(
            "posting_runtime_ready user=%s internship_channel=%s new_grad_channel=%s poll_interval_seconds=%s",
            self.user,
            self.config.discord_channel_id,
            self.config.discord_new_grad_channel_id,
            self.config.poll_interval_seconds,
        )
        if not self._guild_commands_synced:
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            self._guild_commands_synced = True
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

    runtime_config_rows = await database.list_runtime_config()
    apply_runtime_channel_overrides(
        config,
        {str(row["key"]): row for row in runtime_config_rows},
    )
    if (
        config.discord_new_grad_channel_id is None
        or config.discord_new_grad_channel_id == config.discord_channel_id
    ):
        logger.warning(
            "new_grad_channel_unconfigured new-grad deliveries will be suppressed until "
            "/set-job-channel configures a separate channel"
        )

    publisher = TrackChannelPublisher(config, database)
    client: FeedDiscordClient
    pipeline = FeedPipeline(config=config, database=database, publisher=publisher)
    client = FeedDiscordClient(
        config=config,
        database=database,
        pipeline=pipeline,
        publisher=publisher,
    )
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
