from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from math import ceil
from typing import Any, Callable, List, Optional, Sequence

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


COMPANIES_PAGE_SIZE = 12
COMPANIES_PREV_EMOJI = "⬅️"
COMPANIES_NEXT_EMOJI = "➡️"
COMPANIES_STOP_EMOJI = "⏹️"
DEFAULT_PAGE_SIZE = 10
RECENT_PAGE_SIZE = 6
HELP_PAGE_SIZE = 8


def user_has_admin_privileges(user: Any) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


def ensure_user_has_admin_privileges(user: Any) -> bool:
    if user_has_admin_privileges(user):
        return True
    raise commands.CheckFailure("This command requires Discord administrator privileges.")


def build_admin_only_check():
    def predicate(ctx: commands.Context) -> bool:
        return ensure_user_has_admin_privileges(ctx.author)

    return commands.check(predicate)


def build_paginated_embed(
    *,
    title: str,
    entries: Sequence[str],
    page_index: int,
    page_size: int,
    description: str = "",
    empty_text: str = "No items to show.",
    color: Optional[discord.Color] = None,
) -> discord.Embed:
    total = len(entries)
    total_pages = max(1, ceil(total / page_size))
    safe_page_index = min(max(page_index, 0), total_pages - 1)
    start = safe_page_index * page_size
    end = min(start + page_size, total)
    page_entries = entries[start:end]

    body = "\n\n".join(page_entries) if page_entries else empty_text
    if description:
        body = f"{description}\n\n{body}" if body else description

    embed = discord.Embed(
        title=title,
        description=body,
        color=color or discord.Color.blurple(),
    )
    embed.set_footer(text=_pagination_footer_text(total, safe_page_index, page_size))
    return embed


def _pagination_footer_text(total: int, page_index: int, page_size: int) -> str:
    total_pages = max(1, ceil(total / page_size))
    if not total:
        return "Page 1/1"
    start = page_index * page_size
    end = min(start + page_size, total)
    return (
        f"Page {page_index + 1}/{total_pages} • "
        f"Showing {start + 1}-{end} of {total} • "
        f"Use reactions to browse"
    )


def _company_categories(row: Any) -> str:
    categories = json.loads(row["categories"] or "[]")
    return ", ".join(categories) if categories else "none"


def build_companies_embed(rows: Sequence[Any], *, page_index: int, query: str = "") -> discord.Embed:
    title = "Watchlist Companies"
    if query:
        title += f" for '{query}'"

    entries = []
    for row in rows:
        entries.append(
            f"`#{row['id']}` **{row['company']}**\n"
            f"`{row['tier']}` • {_company_categories(row)}"
        )

    return build_paginated_embed(
        title=title,
        entries=entries,
        page_index=page_index,
        page_size=COMPANIES_PAGE_SIZE,
        empty_text="No companies matched that query.",
        color=discord.Color.blurple(),
    )


def build_recent_jobs_embed(
    jobs: Sequence[Any],
    *,
    page_index: int,
    page_size: int = RECENT_PAGE_SIZE,
) -> discord.Embed:
    total = len(jobs)
    total_pages = max(1, ceil(total / page_size))
    safe_page_index = min(max(page_index, 0), total_pages - 1)
    start = safe_page_index * page_size
    end = min(start + page_size, total)
    page_jobs = jobs[start:end]

    embed = discord.Embed(
        title="Recent Jobs",
        description="Tap a role title to open the job listing.",
        color=discord.Color.green(),
    )

    if not page_jobs:
        embed.description = "No jobs stored yet."
    else:
        for display_index, job in enumerate(page_jobs, start=start + 1):
            company = job.company or "Unknown"
            track = job.track.replace("_", " ").title()
            role_title = job.title or "Open role"
            role_link = f"[{role_title}]({job.canonical_url})" if job.canonical_url else role_title
            detail_parts = [job.location or "Unknown"]
            if job.season_term:
                detail_parts.append(job.season_term)
            detail_parts.append(f"Found {job.first_seen.strftime('%Y-%m-%d')}")
            embed.add_field(
                name=f"{display_index}. {company} • {track}",
                value=f"{role_link}\n" + " • ".join(detail_parts),
                inline=False,
            )

    embed.set_footer(text=_pagination_footer_text(total, safe_page_index, page_size))
    return embed


def _format_scan_timestamp(value: str, *, default: str) -> str:
    if not value:
        return default
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    unix_timestamp = int(timestamp.timestamp())
    return f"<t:{unix_timestamp}:f> • <t:{unix_timestamp}:R>"


def _format_company_target(target: Any) -> str:
    target_type = str(target["target_type"]).replace("_", " ").title()
    try:
        locator = json.loads(target["locator"] or "{}")
    except json.JSONDecodeError:
        locator = target["locator"] or ""

    if isinstance(locator, dict):
        locator_url = locator.get("url")
        if locator_url:
            return f"`{target_type}` [Open target]({locator_url})"

        host = locator.get("host")
        site = locator.get("site")
        token = locator.get("token")
        pieces = [part for part in [host, site or token] if part]
        if pieces:
            return f"`{target_type}` " + " / ".join(pieces)

    if isinstance(locator, str) and locator.startswith(("http://", "https://")):
        return f"`{target_type}` [Open target]({locator})"

    return f"`{target_type}` {locator}"


def build_company_info_embed(company: Any, targets: Sequence[Any]) -> discord.Embed:
    categories = ", ".join(json.loads(company["categories"] or "[]")) or "none"
    tracks = ", ".join(json.loads(company["tracks"] or "[]")) or "none"
    description_lines = [
        f"`#{company['id']}` • Tier `{company['tier']}` • Tracks `{tracks}`",
        f"Categories: {categories}",
    ]

    embed = discord.Embed(
        title=company["company"],
        url=company["careers_url"] or company["homepage_url"] or None,
        description="\n".join(description_lines),
        color=discord.Color.teal(),
    )

    if company["logo_url"]:
        embed.set_thumbnail(url=company["logo_url"])

    links_lines = [f"Careers: [Open careers site]({company['careers_url']})"]
    if company["homepage_url"]:
        links_lines.append(f"Homepage: [Open homepage]({company['homepage_url']})")
    else:
        links_lines.append("Homepage: none")
    embed.add_field(name="Links", value="\n".join(links_lines), inline=False)

    branding_lines = [f"Source: `{company['logo_source'] or 'none'}`"]
    if company["logo_url"]:
        branding_lines.append(f"Logo: [Open logo asset]({company['logo_url']})")
    else:
        branding_lines.append("Logo: none")
    embed.add_field(name="Branding", value="\n".join(branding_lines), inline=False)

    embed.add_field(
        name="Scan",
        value="\n".join(
            [
                f"Cadence: `{company['scan_cadence']}`",
                f"Last: {_format_scan_timestamp(company['last_scan_at'], default='never')}",
                f"Next: {_format_scan_timestamp(company['next_scan_at'], default='now')}",
            ]
        ),
        inline=False,
    )

    target_lines = [_format_company_target(target) for target in targets[:8]]
    if len(targets) > 8:
        target_lines.append(f"...and {len(targets) - 8} more")
    embed.add_field(
        name="Targets",
        value="\n".join(target_lines) if target_lines else "none",
        inline=False,
    )
    return embed


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
    seed_stats = await watchlist_service.sync_seed(force_brand_refresh=False)
    startup_branding_refresh_companies = seed_stats.get("branding_updated_companies", [])
    logger.info(
        "Watchlist seed sync complete: rows=%s companies=%s targets=%s logos=%s",
        seed_stats["rows"],
        seed_stats["companies_upserted"],
        seed_stats["targets_upserted"],
        seed_stats.get("logos_verified", 0),
    )

    publisher = DiscordPublisher(config, database)
    pipeline = JobPipeline(config, database, publisher)
    scheduler = BotScheduler(config, pipeline)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
    publisher.bind_bot(bot)

    async def _refresh_branding_posts(normalized_companies: Sequence[str]) -> int:
        jobs = await database.get_posted_canonical_jobs_for_companies(normalized_companies)
        if not jobs:
            return 0
        refreshed = await publisher.refresh_jobs(jobs)
        return len(refreshed)

    @bot.event
    async def on_ready() -> None:
        logger.info("Bot ready as %s", bot.user)
        scheduler.start()
        if startup_branding_refresh_companies:
            refreshed_count = await _refresh_branding_posts(startup_branding_refresh_companies)
            if refreshed_count:
                logger.info(
                    "Refreshed %s posted jobs after branding sync",
                    refreshed_count,
                )

    async def _send_paginated_embed_pages(
        ctx: commands.Context,
        *,
        total_items: int,
        page_size: int = DEFAULT_PAGE_SIZE,
        build_page: Callable[[int], discord.Embed],
    ) -> None:
        message = await ctx.send(embed=build_page(0))
        if total_items <= page_size:
            return

        controls = [COMPANIES_PREV_EMOJI, COMPANIES_NEXT_EMOJI, COMPANIES_STOP_EMOJI]
        try:
            for emoji in controls:
                await message.add_reaction(emoji)
        except discord.HTTPException:
            return

        current_page = 0
        total_pages = ceil(total_items / page_size)

        def _check(reaction: discord.Reaction, user: discord.abc.User) -> bool:
            return (
                reaction.message.id == message.id
                and not user.bot
                and str(reaction.emoji) in controls
            )

        while True:
            try:
                reaction, user = await bot.wait_for("reaction_add", timeout=120.0, check=_check)
            except asyncio.TimeoutError:
                with contextlib.suppress(discord.HTTPException):
                    await message.clear_reactions()
                break

            emoji = str(reaction.emoji)
            if emoji == COMPANIES_STOP_EMOJI:
                with contextlib.suppress(discord.HTTPException):
                    await message.clear_reactions()
                break
            if emoji == COMPANIES_PREV_EMOJI:
                current_page = (current_page - 1) % total_pages
            elif emoji == COMPANIES_NEXT_EMOJI:
                current_page = (current_page + 1) % total_pages

            await message.edit(embed=build_page(current_page))
            with contextlib.suppress(discord.HTTPException):
                await message.remove_reaction(reaction.emoji, user)

    async def _send_paginated_entries(
        ctx: commands.Context,
        *,
        title: str,
        entries: Sequence[str],
        page_size: int = DEFAULT_PAGE_SIZE,
        description: str = "",
        empty_text: str = "No items to show.",
        color: Optional[discord.Color] = None,
    ) -> None:
        await _send_paginated_embed_pages(
            ctx,
            total_items=len(entries),
            page_size=page_size,
            build_page=lambda page_index: build_paginated_embed(
                title=title,
                entries=entries,
                page_index=page_index,
                page_size=page_size,
                description=description,
                empty_text=empty_text,
                color=color,
            ),
        )

    # Restricted commands use a real command check instead of an in-command guard so
    # unauthorized users never trigger any side effects or interim "working..." messages.
    restricted_command = build_admin_only_check()

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
    @restricted_command
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
    @restricted_command
    async def stats_command(ctx: commands.Context) -> None:
        stats = await database.get_stats()
        embed = discord.Embed(title="Job Stats", color=discord.Color.blue())
        embed.add_field(name="Canonical jobs", value=str(stats["total_canonical_jobs"]), inline=True)
        embed.add_field(name="Variants seen", value=str(stats["total_jobs_found"]), inline=True)
        embed.add_field(name="Posted", value=str(stats["total_posted"]), inline=True)
        embed.add_field(name="Deduplicated", value=str(stats["total_deduplicated"]), inline=True)
        await ctx.send(embed=embed)

    @bot.command(name="sources")
    @restricted_command
    async def sources_command(ctx: commands.Context) -> None:
        entries = []
        for source in build_sources(config):
            status = "enabled" if source.settings.enabled else "disabled"
            experimental = " experimental" if source.experimental else ""
            entries.append(
                f"**{source.name}**\n"
                f"`{status}` • phase `{source.phase}` • priority `{source.priority}`{experimental}"
            )
        await _send_paginated_entries(
            ctx,
            title="Configured Sources",
            entries=entries,
            page_size=DEFAULT_PAGE_SIZE,
            empty_text="No sources configured.",
            color=discord.Color.dark_teal(),
        )

    @bot.command(name="recent")
    async def recent_command(ctx: commands.Context) -> None:
        jobs = await database.get_recent_jobs(limit=100)
        if not jobs:
            await ctx.send("No jobs stored yet.")
            return
        await _send_paginated_embed_pages(
            ctx,
            total_items=len(jobs),
            page_size=RECENT_PAGE_SIZE,
            build_page=lambda page_index: build_recent_jobs_embed(
                jobs,
                page_index=page_index,
                page_size=RECENT_PAGE_SIZE,
            ),
        )

    @bot.command(name="dedupecheck")
    @restricted_command
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
    @restricted_command
    async def season_command(ctx: commands.Context, *, value: str = "") -> None:
        configured = ", ".join(config.season_filter) if config.season_filter else "none"
        if value:
            await ctx.send(
                f"Season filter is configured via environment. Current value: {configured}"
            )
            return
        await ctx.send(f"Current season filter: {configured}")

    @bot.command(name="summary")
    @restricted_command
    async def summary_command(ctx: commands.Context, period: str = "daily") -> None:
        hours = 24 if period.lower() == "daily" else 24 * 7
        jobs = await database.get_jobs_for_summary(hours)
        if not jobs:
            await ctx.send("No jobs in that window.")
            return
        tracks = Counter(job.track for job in jobs)
        companies = Counter(job.company for job in jobs).most_common(5)
        embed = discord.Embed(
            title=f"Summary: last {hours}h",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Total jobs", value=str(len(jobs)), inline=True)
        embed.add_field(name="Internships", value=str(tracks.get("internship", 0)), inline=True)
        embed.add_field(name="New grad", value=str(tracks.get("new_grad", 0)), inline=True)
        embed.add_field(
            name="Top companies",
            value=", ".join(f"{name} ({count})" for name, count in companies) or "none",
            inline=False,
        )
        await ctx.send(embed=embed)

    async def _send_helpjobs(ctx: commands.Context) -> None:
        entries = [
            "`!ping` - health check",
            "`!help` - show this help message",
            "`!runjobs` - trigger a manual discovery run [admin only]",
            "`!stats` - show stored job stats [admin only]",
            "`!sources` - list configured sources [admin only]",
            "`!recent` - show the most recent jobs",
            "`!requestcompany <company> | <careers_url> | [homepage_url]` - request a company",
            "`!companyrequests [pending|approved|denied|all]` - request queue [admin only]",
            "`!approvecompany <request_id>` - approve a company request [admin only]",
            "`!denycompany <request_id> [reason]` - deny a company request [admin only]",
            "`!companies [query]` - list watchlist companies",
            "`!companyinfo <company|id>` - show watchlist details",
            "`!synccompanies` - reimport the company watchlist seed [admin only]",
            "`!dedupecheck <url or text>` - test duplicate detection [admin only]",
            "`!season [value]` - show season filter configuration [admin only]",
            "`!summary [daily|weekly]` - show recent summary stats [admin only]",
        ]
        await _send_paginated_entries(
            ctx,
            title="Command Help",
            entries=entries,
            page_size=HELP_PAGE_SIZE,
            color=discord.Color.purple(),
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
            await ctx.send(
                "Use `!requestcompany Company Name | https://company.com/careers | [https://company.com]`"
            )
            return
        parts = [part.strip() for part in value.split("|")]
        if len(parts) < 2 or len(parts) > 3:
            await ctx.send(
                "Use `!requestcompany Company Name | https://company.com/careers | [https://company.com]`"
            )
            return
        company, careers_url = parts[0], parts[1]
        homepage_url = parts[2] if len(parts) == 3 else ""
        try:
            request_id = await watchlist_service.request_company(
                company=company,
                careers_url=careers_url,
                homepage_url=homepage_url,
                requester_user_id=str(ctx.author.id),
                requester_channel_id=str(ctx.channel.id),
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(f"Request submitted as #{request_id}: {company}")

    @bot.command(name="companyrequests")
    @restricted_command
    async def companyrequests(ctx: commands.Context, status: str = "pending") -> None:
        requests = await watchlist_service.list_requests(status=status if status != "all" else None)
        if not requests:
            await ctx.send("No company requests found.")
            return
        entries = [
            f"`#{row['id']}` **{row['company']}**\n"
            f"`{row['status']}` • {row['careers_url']}"
            for row in requests
        ]
        await _send_paginated_entries(
            ctx,
            title="Company Requests",
            entries=entries,
            page_size=DEFAULT_PAGE_SIZE,
            color=discord.Color.orange(),
        )

    @bot.command(name="approvecompany")
    @restricted_command
    async def approvecompany(ctx: commands.Context, request_id: int) -> None:
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
    @restricted_command
    async def denycompany(ctx: commands.Context, request_id: int, *, reason: str = "Denied") -> None:
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
        rows = await watchlist_service.list_companies(query=query, limit=1000)
        if not rows:
            await ctx.send("No companies matched that query.")
            return

        entries = [
            f"`#{row['id']}` **{row['company']}**\n"
            f"`{row['tier']}` • {_company_categories(row)}"
            for row in rows
        ]
        title = "Watchlist Companies"
        if query:
            title += f" for '{query}'"
        await _send_paginated_entries(
            ctx,
            title=title,
            entries=entries,
            page_size=COMPANIES_PAGE_SIZE,
            empty_text="No companies matched that query.",
            color=discord.Color.blurple(),
        )

    @bot.command(name="companyinfo")
    async def companyinfo(ctx: commands.Context, *, query: str) -> None:
        result = await watchlist_service.get_company_info(query)
        if not result:
            await ctx.send("Company not found in watchlist.")
            return
        company = result["company"]
        targets = result["targets"]
        await ctx.send(embed=build_company_info_embed(company, targets))

    @bot.command(name="synccompanies")
    @restricted_command
    async def synccompanies(ctx: commands.Context) -> None:
        stats = await watchlist_service.sync_seed(force_brand_refresh=True)
        refreshed_posts = await _refresh_branding_posts(stats.get("branding_updated_companies", []))
        await ctx.send(
            f"Watchlist sync complete. rows={stats['rows']} companies={stats['companies_upserted']} "
            f"targets={stats['targets_upserted']} logos={stats.get('logos_verified', 0)} "
            f"refreshed_posts={refreshed_posts}"
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
