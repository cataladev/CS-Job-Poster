from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands

from job_bot.ats_feeds import build_direct_ats_feeds
from job_bot.company_sources.runner import CompanySourcePipeline
from job_bot.config import (
    apply_runtime_channel_overrides,
    configure_logging,
    load_config,
)
from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline
from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.github_feeds import FEEDS, GitHubFeedFetcher
from job_bot.health_commands import register_health_commands
from job_bot.job_channel_commands import (
    JobChannelConfigurator,
    register_job_channel_commands,
)
from job_bot.log_threads import CoreLogNotifier, LogThreads
from job_bot.models import AppConfig
from job_bot.scrape_diagnostics import ScrapeFailure
from job_bot.scrape_status import ScrapeStatusReporter


# Bump this whenever a release changes what a source can see -- a wider query, a new
# parser, a corrected filter. Every row that becomes newly visible would otherwise look
# like a job posted moments ago, and the whole backlog would be delivered at once. A new
# epoch makes the next collection record everything in silence and deliver only what
# appears after it. Sent deliveries are never replayed.
FEED_BASELINE_EPOCH = "2026-09-02-jobright-employer-links"

CHANNEL_RECOVERY_KEY = "restore_established_job_channels_20260819_v1"
MISROUTED_CHANNELS = {
    "internship": 1539496312636969020,
    "new_grad": 1539496313102540800,
}
ESTABLISHED_CHANNELS = {
    "internship": 1537949209867583499,
    "new_grad": 1537949230742900827,
}
CHANNEL_REDIRECTED_AT = "2026-08-19T04:48:46+00:00"


class FeedDiscordClient(discord.Client):
    def __init__(
        self,
        *,
        config: AppConfig,
        database: Database,
        pipeline: FeedPipeline,
        company_source_pipeline: CompanySourcePipeline,
        publisher: TrackChannelPublisher,
        log_threads: LogThreads | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = False
        # Nothing pings by default. Every mention this bot sends is an explicit
        # allowlist at the send site (one digest role, or the members targeting a
        # job), so @everyone and @here can never be reached -- not by a job title, a
        # company name, or an upstream error string that happens to contain them.
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.pipeline = pipeline
        self.company_source_pipeline = company_source_pipeline
        self.publisher = publisher
        self.log_threads = log_threads or LogThreads(config)
        self.scrape_status_reporter = ScrapeStatusReporter(config, log_threads=self.log_threads)
        self.scrape_status_reporter.bind_client(self)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.tree = app_commands.CommandTree(self)
        self.channel_configurator = JobChannelConfigurator(config, database)
        register_job_channel_commands(
            self.tree,
            configurator=self.channel_configurator,
            publisher=publisher,
        )
        register_health_commands(
            self.tree,
            database=database,
            config=config,
            publisher=publisher,
        )
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown = asyncio.Event()
        self._guild_commands_synced = False

    async def on_ready(self) -> None:
        posting_guild_id = await self.publisher.configured_guild_id()
        self.logger.info(
            "posting_runtime_ready user=%s guild=%s internship_channel=%s "
            "new_grad_channel=%s poll_interval_seconds=%s",
            self.user,
            posting_guild_id,
            self.config.discord_channel_id,
            self.config.discord_new_grad_channel_id,
            self.config.poll_interval_seconds,
        )
        if not self._guild_commands_synced:
            await self._sync_commands_to_guild(posting_guild_id)
            self._guild_commands_synced = True
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="new U.S. jobs",
            ),
        )
        self._ensure_category_task()
        self._ensure_log_thread_task()
        self._ensure_poll_task(
            name="core",
            pipeline=self.pipeline,
            interval_seconds=self.config.poll_interval_seconds,
        )
        self._ensure_poll_task(
            name="company_sources",
            pipeline=self.company_source_pipeline,
            interval_seconds=self.config.company_source_poll_interval_seconds,
        )

    async def _sync_commands_to_guild(self, guild_id: int) -> None:
        guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=guild)
        self.tree.clear_commands(guild=None)

        # Guild commands update immediately. Syncing the now-empty global command
        # set removes commands published by earlier versions of the runtime.
        await self.tree.sync(guild=guild)
        await self.tree.sync()

        # Remove stale guild-scoped commands from any other servers where an
        # earlier version copied them.
        for connected_guild in self.guilds:
            if connected_guild.id == guild_id:
                continue
            self.tree.clear_commands(guild=connected_guild)
            await self.tree.sync(guild=connected_guild)

    async def close(self) -> None:
        self._shutdown.set()
        poll_tasks = list(self._poll_tasks.values())
        for poll_task in poll_tasks:
            poll_task.cancel()
        for poll_task in poll_tasks:
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        await super().close()

    def _ensure_log_thread_task(self) -> None:
        task = self._poll_tasks.get("log_threads")
        if task is None or task.done():
            self._poll_tasks["log_threads"] = asyncio.create_task(
                self.log_threads.ensure_threads(), name="log-thread-setup",
            )

    def _ensure_category_task(self) -> None:
        task = self._poll_tasks.get("categories")
        if task is None or task.done():
            self._poll_tasks["categories"] = asyncio.create_task(
                self.publisher.outsource_mirror.ensure_categories(),
                name="consolidated-category-setup",
            )

    def _ensure_poll_task(
        self,
        *,
        name: str,
        pipeline: FeedPipeline | CompanySourcePipeline,
        interval_seconds: int,
    ) -> None:
        poll_task = self._poll_tasks.get(name)
        if poll_task is None or poll_task.done():
            self._poll_tasks[name] = asyncio.create_task(
                self._poll_loop(
                    pipeline=pipeline,
                    interval_seconds=interval_seconds,
                    feed_group=name,
                ),
                name=f"{name}-feed-poller",
            )

    async def _poll_loop(
        self,
        *,
        pipeline: FeedPipeline | CompanySourcePipeline,
        interval_seconds: int,
        feed_group: str,
    ) -> None:
        while not self.is_closed() and not self._shutdown.is_set():
            started = time.monotonic()
            unexpected_error: Exception | None = None
            try:
                stats = await pipeline.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = ScrapeFailure.from_exception(exc)
                self.logger.exception(
                    "feed_poll_failed feed_group=%s source=%s stage=%s error_type=%s error=%s",
                    feed_group,
                    failure.source or "shared_pipeline",
                    failure.stage,
                    failure.error_type,
                    failure.message,
                )
                stats = getattr(exc, "scrape_stats", None)
                unexpected_error = exc
            elapsed = time.monotonic() - started
            try:
                await self.scrape_status_reporter.report(
                    feed_group=feed_group,
                    elapsed_seconds=elapsed,
                    stats=stats,
                    unexpected_error=unexpected_error,
                    poll_interval_seconds=interval_seconds,
                )
            except Exception:
                self.logger.exception(
                    "scrape_status_report_failed feed_group=%s",
                    feed_group,
                )
            delay = max(1.0, interval_seconds - elapsed)
            self.logger.info(
                "feed_poll_scheduled feed_group=%s interval_seconds=%s elapsed_seconds=%.2f "
                "next_poll_seconds=%.2f sent=%s errors=%s",
                feed_group,
                interval_seconds,
                elapsed,
                delay,
                stats.deliveries_sent if stats is not None else 0,
                len(ScrapeStatusReporter._errors(stats, unexpected_error)),
            )
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue


async def run_bot() -> None:
    config = load_config()
    configure_logging(config.log_level, config.bot_environment)
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

    baseline_epoch = await database.apply_feed_baseline_epoch(FEED_BASELINE_EPOCH)
    if any(baseline_epoch.values()):
        logger.warning(
            "feed_baseline_epoch_applied epoch=%s feeds_reset=%s deliveries_suppressed=%s "
            "-- the next collection records every source silently and delivers only "
            "what appears after it",
            FEED_BASELINE_EPOCH,
            baseline_epoch["feeds_reset"],
            baseline_epoch["deliveries_suppressed"],
        )

    recovery = await database.recover_misrouted_delivery_channels(
        recovery_key=CHANNEL_RECOVERY_KEY,
        source_channels=MISROUTED_CHANNELS,
        target_channels=ESTABLISHED_CHANNELS,
        replay_sent_after=CHANNEL_REDIRECTED_AT,
    )
    if any(recovery.values()):
        logger.warning(
            "discord_channel_redirect_recovered channels_restored=%s deliveries_moved=%s "
            "deliveries_requeued=%s",
            recovery["channels_restored"],
            recovery["deliveries_moved"],
            recovery["deliveries_requeued"],
        )

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
    log_threads = LogThreads(config)
    client: FeedDiscordClient
    direct_link_feeds = build_direct_ats_feeds(
        config.company_seed_path,
        poll_interval_seconds=14_400,
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=publisher,
        feeds=FEEDS,
        direct_link_feeds=direct_link_feeds,
        fetcher=GitHubFeedFetcher(jobright_notifier=CoreLogNotifier(log_threads)),
    )
    company_source_pipeline = CompanySourcePipeline(
        config=config,
        database=database,
        feed_pipeline=pipeline,
    )
    logger.info(
        "bounded_direct_link_resolver_enabled boards=%s cache_ttl_seconds=%s",
        len(direct_link_feeds),
        14_400,
    )
    client = FeedDiscordClient(
        config=config,
        database=database,
        pipeline=pipeline,
        company_source_pipeline=company_source_pipeline,
        publisher=publisher,
        log_threads=log_threads,
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
