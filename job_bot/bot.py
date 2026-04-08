from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import List

import discord
from discord.ext import commands
from rapidfuzz import fuzz

from job_bot.config import configure_logging, load_config
from job_bot.database import Database
from job_bot.pipeline import JobPipeline
from job_bot.publisher import DiscordPublisher
from job_bot.scheduler import BotScheduler
from job_bot.sources import build_sources
from job_bot.utils.normalization import canonicalize_url


async def run_bot() -> None:
    config = load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger("bot")

    if not config.discord_token:
        raise RuntimeError("DISCORD_TOKEN is required")
    if not config.discord_channel_id:
        raise RuntimeError("DISCORD_CHANNEL_ID is required")

    database = Database(config.sqlite_db_path)
    await database.initialize()

    publisher = DiscordPublisher(config, database)
    pipeline = JobPipeline(config, database, publisher)
    scheduler = BotScheduler(config, pipeline)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
    publisher.bind_bot(bot)

    @bot.event
    async def on_ready() -> None:
        logger.info("Bot ready as %s", bot.user)
        scheduler.start()

    @bot.command(name="ping")
    async def ping(ctx: commands.Context) -> None:
        await ctx.send("pong")

    @bot.command(name="runjobs")
    async def runjobs(ctx: commands.Context) -> None:
        message = await ctx.send(
            "Running job discovery pipeline. New jobs will post as sources finish."
        )
        stats = await pipeline.run(trigger="manual")
        await ctx.send(
            (
                f"Run complete. fetched={stats.jobs_fetched} filtered={stats.jobs_filtered} "
                f"duplicates={stats.duplicates_removed} posted={stats.jobs_posted} "
                f"errors={len(stats.errors)}"
            )
        )
        await message.delete()

    @bot.command(name="stats")
    async def stats_command(ctx: commands.Context) -> None:
        stats = await database.get_stats()
        await ctx.send(
            "\n".join(
                [
                    f"Canonical jobs: {stats['total_canonical_jobs']}",
                    f"Variants seen: {stats['total_jobs_found']}",
                    f"Posted: {stats['total_posted']}",
                    f"Deduplicated: {stats['total_deduplicated']}",
                ]
            )
        )

    @bot.command(name="sources")
    async def sources_command(ctx: commands.Context) -> None:
        lines = []
        for source in build_sources(config):
            status = "enabled" if source.settings.enabled else "disabled"
            experimental = " experimental" if source.experimental else ""
            lines.append(f"{source.name}: {status}, phase={source.phase}{experimental}")
        await ctx.send("\n".join(lines) if lines else "No sources configured.")

    @bot.command(name="recent")
    async def recent_command(ctx: commands.Context) -> None:
        jobs = await database.get_recent_jobs(limit=10)
        if not jobs:
            await ctx.send("No jobs stored yet.")
            return
        lines = [
            f"[{job.track}] {job.company} - {job.title} ({job.location})"
            for job in jobs
        ]
        await ctx.send("\n".join(lines))

    @bot.command(name="dedupecheck")
    async def dedupecheck_command(ctx: commands.Context, *, query: str) -> None:
        query = query.strip()
        if query.startswith("http://") or query.startswith("https://"):
            canonical = canonicalize_url(query)
            match = await database.find_by_url(canonical)
            if match:
                await ctx.send(
                    f"Duplicate of [{match.track}] {match.company} - {match.title} ({match.location})"
                )
            else:
                await ctx.send("No exact URL duplicate found.")
            return

        candidates = await database.search_text(query, limit=20)
        if not candidates:
            await ctx.send("No likely duplicates found.")
            return
        best = max(
            candidates,
            key=lambda job: fuzz.token_set_ratio(
                query.lower(),
                f"{job.company} {job.title} {job.location}".lower(),
            ),
        )
        score = fuzz.token_set_ratio(
            query.lower(),
            f"{best.company} {best.title} {best.location}".lower(),
        )
        if score < 70:
            await ctx.send("No likely duplicates found.")
            return
        await ctx.send(
            f"Likely duplicate ({score:.1f}) of [{best.track}] {best.company} - {best.title} ({best.location})"
        )

    @bot.command(name="season")
    async def season_command(ctx: commands.Context, *, value: str = "") -> None:
        configured = ", ".join(config.season_filter) if config.season_filter else "none"
        if value:
            await ctx.send(
                f"Season filter is configured via environment. Current value: {configured}"
            )
            return
        await ctx.send(f"Current season filter: {configured}")

    @bot.command(name="summary")
    async def summary_command(ctx: commands.Context, period: str = "daily") -> None:
        hours = 24 if period.lower() == "daily" else 24 * 7
        jobs = await database.get_jobs_for_summary(hours)
        if not jobs:
            await ctx.send("No jobs in that window.")
            return
        tracks = Counter(job.track for job in jobs)
        companies = Counter(job.company for job in jobs).most_common(5)
        lines = [
            f"Window: last {hours}h",
            f"Total jobs: {len(jobs)}",
            f"Internships: {tracks.get('internship', 0)}",
            f"New grad: {tracks.get('new_grad', 0)}",
            "Top companies: " + ", ".join(f"{name} ({count})" for name, count in companies),
        ]
        await ctx.send("\n".join(lines))

    @bot.command(name="helpjobs")
    async def helpjobs(ctx: commands.Context) -> None:
        await ctx.send(
            "\n".join(
                [
                    "!ping - health check",
                    "!runjobs - trigger a manual discovery run",
                    "!stats - show stored job stats",
                    "!sources - list configured sources",
                    "!recent - show the most recent jobs",
                    "!dedupecheck <url or text> - test duplicate detection",
                    "!season [value] - show season filter configuration",
                    "!summary [daily|weekly] - show recent summary stats",
                ]
            )
        )

    try:
        await bot.start(config.discord_token)
    finally:
        scheduler.shutdown()
        await database.close()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
