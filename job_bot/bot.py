from __future__ import annotations

import asyncio
import json
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
from job_bot.watchlist import WatchlistService


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
    watchlist_service = WatchlistService(config, database)
    seed_stats = await watchlist_service.sync_seed()
    logger.info(
        "Watchlist seed sync complete: rows=%s companies=%s targets=%s",
        seed_stats["rows"],
        seed_stats["companies_upserted"],
        seed_stats["targets_upserted"],
    )

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

    def _is_admin(ctx: commands.Context) -> bool:
        if getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.manage_guild:
            return True
        author_role_ids = {role.id for role in getattr(ctx.author, "roles", [])}
        return bool(author_role_ids.intersection(set(config.discord_admin_role_ids)))

    def _require_admin(ctx: commands.Context) -> bool:
        if _is_admin(ctx):
            return True
        raise commands.CheckFailure("Admin access required.")

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error))
            return
        if isinstance(error, commands.CommandNotFound):
            return
        raise error

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

    async def _send_helpjobs(ctx: commands.Context) -> None:
        await ctx.send(
            "\n".join(
                [
                    "!ping - health check",
                    "!help - show this help message",
                    "!runjobs - trigger a manual discovery run",
                    "!stats - show stored job stats",
                    "!sources - list configured sources",
                    "!recent - show the most recent jobs",
                    "!requestcompany <company> | <careers_url> - request a company",
                    "!companyrequests [pending|approved|denied|all] - admin request queue",
                    "!approvecompany <request_id> - approve a company request",
                    "!denycompany <request_id> [reason] - deny a company request",
                    "!companies [query] - list watchlist companies",
                    "!companyinfo <company|id> - show watchlist details",
                    "!synccompanies - reimport the company watchlist seed",
                    "!dedupecheck <url or text> - test duplicate detection",
                    "!season [value] - show season filter configuration",
                    "!summary [daily|weekly] - show recent summary stats",
                ]
            )
        )

    @bot.command(name="help")
    async def help_command(ctx: commands.Context) -> None:
        await _send_helpjobs(ctx)

    @bot.command(name="helpjobs")
    async def helpjobs(ctx: commands.Context) -> None:
        await _send_helpjobs(ctx)

    @bot.command(name="requestcompany")
    async def requestcompany(ctx: commands.Context, *, value: str) -> None:
        if "|" not in value:
            await ctx.send("Use `!requestcompany Company Name | https://company.com/careers`")
            return
        company, careers_url = [part.strip() for part in value.split("|", 1)]
        try:
            request_id = await watchlist_service.request_company(
                company=company,
                careers_url=careers_url,
                requester_user_id=str(ctx.author.id),
                requester_channel_id=str(ctx.channel.id),
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(f"Request submitted as #{request_id}: {company}")

    @bot.command(name="companyrequests")
    async def companyrequests(ctx: commands.Context, status: str = "pending") -> None:
        _require_admin(ctx)
        requests = await watchlist_service.list_requests(status=status if status != "all" else None)
        if not requests:
            await ctx.send("No company requests found.")
            return
        lines = [
            f"#{row['id']} [{row['status']}] {row['company']} | {row['careers_url']}"
            for row in requests
        ]
        await ctx.send("\n".join(lines[:20]))

    @bot.command(name="approvecompany")
    async def approvecompany(ctx: commands.Context, request_id: int) -> None:
        _require_admin(ctx)
        try:
            company = await watchlist_service.approve_request(
                request_id=request_id,
                approver_user_id=str(ctx.author.id),
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(f"Approved {company['company']} into the watchlist.")

    @bot.command(name="denycompany")
    async def denycompany(ctx: commands.Context, request_id: int, *, reason: str = "Denied") -> None:
        _require_admin(ctx)
        try:
            await watchlist_service.deny_request(
                request_id=request_id,
                approver_user_id=str(ctx.author.id),
                reason=reason,
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(f"Denied request #{request_id}.")

    @bot.command(name="companies")
    async def companies(ctx: commands.Context, *, query: str = "") -> None:
        rows = await watchlist_service.list_companies(query=query, limit=20)
        if not rows:
            await ctx.send("No companies matched that query.")
            return
        lines = []
        for row in rows:
            categories = ", ".join(json.loads(row["categories"] or "[]"))
            lines.append(
                f"#{row['id']} {row['company']} [{row['tier']}] {categories} | {row['careers_url']}"
            )
        await ctx.send("\n".join(lines))

    @bot.command(name="companyinfo")
    async def companyinfo(ctx: commands.Context, *, query: str) -> None:
        result = await watchlist_service.get_company_info(query)
        if not result:
            await ctx.send("Company not found in watchlist.")
            return
        company = result["company"]
        targets = result["targets"]
        target_lines = []
        for target in targets[:10]:
            locator = json.loads(target["locator"])
            display = locator.get("url") or locator.get("token") or locator
            target_lines.append(f"{target['target_type']}: {display}")
        await ctx.send(
            "\n".join(
                [
                    f"#{company['id']} {company['company']}",
                    f"Tier: {company['tier']}",
                    f"Categories: {', '.join(json.loads(company['categories'] or '[]'))}",
                    f"Tracks: {', '.join(json.loads(company['tracks'] or '[]'))}",
                    f"Careers: {company['careers_url']}",
                    f"Last scan: {company['last_scan_at'] or 'never'}",
                    f"Next scan: {company['next_scan_at'] or 'now'}",
                    "Targets: " + (" | ".join(target_lines) if target_lines else "none"),
                ]
            )
        )

    @bot.command(name="synccompanies")
    async def synccompanies(ctx: commands.Context) -> None:
        _require_admin(ctx)
        stats = await watchlist_service.sync_seed()
        await ctx.send(
            f"Watchlist sync complete. rows={stats['rows']} companies={stats['companies_upserted']} "
            f"targets={stats['targets_upserted']}"
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
