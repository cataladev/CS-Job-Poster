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
JOB_PEER_PAGE_SIZE = 10
MY_JOBS_PAGE_SIZE = 10

JOB_VISIBILITY_DEFAULT = "matched"
JOB_VISIBILITY_LABELS = {
    "matched": "Matched Only",
    "open": "Open",
    "private": "Private",
}
JOB_STATUS_LABELS = {
    "save": "Saved",
    "hide": "Hidden",
    "applied": "Applied",
    "oa_received": "OA Received",
    "interview": "Interview",
    "rejected": "Rejected",
    "offer": "Offer",
}
JOB_STATUS_ALIASES = {
    "save": "save",
    "saved": "save",
    "bookmark": "save",
    "bookmarked": "save",
    "hide": "hide",
    "hidden": "hide",
    "applied": "applied",
    "apply": "applied",
    "application_sent": "applied",
    "oa": "oa_received",
    "oa_received": "oa_received",
    "oareceived": "oa_received",
    "online_assessment": "oa_received",
    "onlineassessment": "oa_received",
    "interview": "interview",
    "interviews": "interview",
    "rejected": "rejected",
    "reject": "rejected",
    "offer": "offer",
    "offered": "offer",
}
JOB_STATUS_ORDER = {
    "save": 0,
    "applied": 1,
    "oa_received": 2,
    "interview": 3,
    "offer": 4,
    "rejected": 5,
    "hide": 6,
}
JOB_COMMUNITY_RULES = "\n".join(
    [
        "Rules:",
        "• Opt-in only",
        "• No OA/interview leaks, screenshots, recruiter emails, or another person's private details",
        "• Use this to find like-minded peers, not to share confidential process content",
    ]
)


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


def normalize_job_visibility(value: str) -> Optional[str]:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "matched": "matched",
        "match": "matched",
        "matched_only": "matched",
        "open": "open",
        "everyone": "open",
        "public": "open",
        "private": "private",
        "hidden": "private",
        "off": "private",
    }
    return aliases.get(cleaned)


def format_job_visibility(visibility: str) -> str:
    return JOB_VISIBILITY_LABELS.get(visibility, visibility.replace("_", " ").title())


def normalize_job_tracking_status(value: str) -> Optional[str]:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    return JOB_STATUS_ALIASES.get(cleaned)


def parse_jobstatus_input(value: str) -> Optional[tuple[str, str]]:
    raw = value.strip()
    if not raw:
        return None
    if "|" in raw:
        company_query, status_input = [part.strip() for part in raw.split("|", 1)]
        status = normalize_job_tracking_status(status_input)
        if company_query and status:
            return company_query, status
        return None

    tokens = raw.split()
    if len(tokens) < 2:
        return None

    for suffix_length in (2, 1):
        if len(tokens) <= suffix_length:
            continue
        company_query = " ".join(tokens[:-suffix_length]).strip()
        status_input = " ".join(tokens[-suffix_length:]).strip()
        status = normalize_job_tracking_status(status_input)
        if company_query and status:
            return company_query, status
    return None


def parse_jobprocess_input(value: str) -> Optional[tuple[str, Optional[str], bool]]:
    raw = value.strip()
    if not raw:
        return None
    if "|" in raw:
        company_query, note = [part.strip() for part in raw.split("|", 1)]
        if not company_query or not note:
            return None
        return company_query, note, False

    lowered = raw.lower()
    for suffix in (" comments", " comment", " notes", " note"):
        if lowered.endswith(suffix) and len(raw) > len(suffix):
            return raw[: -len(suffix)].strip(), None, True
    return raw, None, False


def parse_process_command_input(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        return "help", ""

    if normalize_job_visibility(raw):
        return "visibility", raw

    head, _, tail = raw.partition(" ")
    action = head.lower()
    action_aliases = {
        "help": "help",
        "menu": "help",
        "me": "myjobs",
        "my": "myjobs",
        "mine": "myjobs",
        "peer": "jobpeers",
        "peers": "jobpeers",
        "visibility": "visibility",
        "vis": "visibility",
        "status": "jobstatus",
        "set": "jobstatus",
        "stat": "jobprocess",
        "stats": "jobprocess",
        "note": "jobprocess",
        "notes": "jobprocess",
        "comment": "jobprocess",
        "comments": "jobprocess",
    }
    if action in action_aliases:
        resolved = action_aliases[action]
        if action in {"note", "notes", "comment", "comments"} and tail:
            return resolved, f"{tail} notes"
        return resolved, tail

    if parse_jobstatus_input(raw):
        return "jobstatus", raw
    return "jobprocess", raw


def format_job_tracking_status(status: str) -> str:
    return JOB_STATUS_LABELS.get(status, status.replace("_", " ").title())


def job_status_sort_key(status: str) -> int:
    return JOB_STATUS_ORDER.get(status, 99)


def job_statuses_help_text() -> str:
    return ", ".join(f"`{name}`" for name in ["save", "hide", "applied", "oa", "interview", "rejected", "offer"])


def jobstatus_usage_text() -> str:
    return (
        f"Use `!process status Company Name status` or `!jobstatus Company Name status` "
        f"where status is one of {job_statuses_help_text()}."
    )


def jobprocess_usage_text() -> str:
    return (
        "Use `!process <company|id>` for company stats, "
        "`!process <company|id> notes` for notes, or "
        "`!process <company|id> | <note>` to save your note."
    )


def build_process_help_message(*, visibility: str, joined: bool) -> str:
    lines = [
        "You're in the job-process community." if joined else "Job process commands",
        f"`Visibility:` {visibility}",
        "",
        "`Use:`",
        "• `!process nvidia oa` set your status",
        "• `!process peers nvidia` find peers",
        "• `!process nvidia` view company process stats",
        "• `!process nvidia notes` view notes",
        "• `!process nvidia | recruiter said 2 rounds` save your note",
        "• `!process me` view your tracker",
        "• `!process open` or `!process visibility open` change discovery",
        "",
        JOB_COMMUNITY_RULES,
    ]
    return "\n".join(lines)


def _clean_job_process_note(note: str) -> str:
    return " ".join(note.strip().split())[:400]


def _latest_process_note_text(row: Any) -> str:
    latest_note = row["latest_note"] if "latest_note" in row.keys() else None
    if not latest_note:
        return ""
    note_count = int(row["note_count"] or 0) if "note_count" in row.keys() else 0
    prefix = "Latest note"
    if note_count > 1:
        prefix += f" ({note_count})"
    return f"\n{prefix}: {latest_note}"


def _job_process_counts_text(rows: Sequence[Any]) -> str:
    counts = Counter(row["status"] for row in rows if row["status"] != "hide")
    ordered_statuses = [status for status, _ in sorted(JOB_STATUS_ORDER.items(), key=lambda item: item[1])]
    parts = [
        f"{format_job_tracking_status(status)} {counts[status]}"
        for status in ordered_statuses
        if counts.get(status)
    ]
    return " • ".join(parts) if parts else "No visible statuses yet."


def _format_relative_timestamp(value: str, *, default: str = "just now") -> str:
    if not value:
        return default
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return f"<t:{int(timestamp.timestamp())}:R>"


def build_recent_jobs_embed(
    jobs: Sequence[Any],
    *,
    page_index: int,
    page_size: int = RECENT_PAGE_SIZE,
    company_display_names: Optional[dict[str, str]] = None,
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
            company = _recent_company_display_name(job, company_display_names)
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


def _is_hot_recent_job(job: Any) -> bool:
    company = job.company or ""
    if "🔥" in company:
        return True
    return any("🔥" in str(tag) for tag in getattr(job, "provenance_tags", []))


def _recent_company_display_name(job: Any, company_display_names: Optional[dict[str, str]] = None) -> str:
    normalized_company = getattr(job, "normalized_company", "")
    if company_display_names and normalized_company in company_display_names:
        return company_display_names[normalized_company]
    return job.company or "Unknown"


def sort_recent_jobs(
    jobs: Sequence[Any],
    company_display_names: Optional[dict[str, str]] = None,
) -> List[Any]:
    return sorted(
        jobs,
        key=lambda job: (
            not (
                "🔥" in _recent_company_display_name(job, company_display_names)
                or _is_hot_recent_job(job)
            ),
            -job.first_seen.timestamp(),
            _recent_company_display_name(job, company_display_names).lower(),
            (job.title or "").lower(),
        ),
    )


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


def build_help_entries() -> List[str]:
    return [
        "`!ping` - health check",
        "`!help` - show the quick help menu",
        "`!helpjobs` - show the full command menu",
        "`!runjobs` - trigger a manual discovery run [admin only]",
        "`!stats` - show stored job stats [admin only]",
        "`!sources` - list configured sources [admin only]",
        "`!recent` - show the most recent jobs",
        "`!requestcompany <company> | <careers_url> | [homepage_url]` - request a company",
        "`!companyrequests [pending|approved|denied|all]` - request queue [admin only]",
        "`!approvecompany <request_id>` - approve a company request [admin only]",
        "`!denycompany <request_id> [reason]` - deny a company request [admin only]",
        "`!firecompany <company|id>` - mark a watchlist company as hot [admin only]",
        "`!companies [query]` - list watchlist companies",
        "`!companyinfo <company|id>` - show watchlist details",
        "`!synccompanies` - reimport the company watchlist seed [admin only]",
        "`!dedupecheck <url or text>` - test duplicate detection [admin only]",
        "`!season [value]` - show season filter configuration [admin only]",
        "`!summary [daily|weekly]` - show recent summary stats [admin only]",
        "`!process` - join the process tracker and show examples",
        "`!process nvidia oa` - set your status",
        "`!process peers nvidia` - find peers",
        "`!process nvidia` - view company process stats",
        "`!process nvidia | recruiter said 2 rounds` - save your note",
    ]


def build_quick_help_embed(*, is_admin: bool) -> discord.Embed:
    embed = discord.Embed(
        title="Help",
        description="Quick command guide. Use `!helpjobs` for the full menu.",
        color=discord.Color.purple(),
    )
    embed.add_field(
        name="Browse",
        value="\n".join(
            [
                "`!recent` recent jobs",
                "`!companies [query]` companies",
                "`!companyinfo <company|id>` company details",
            ]
        ),
        inline=False,
    )
    embed.add_field(
        name="Requests",
        value="`!requestcompany <company> | <careers_url> | [homepage_url]`",
        inline=False,
    )
    embed.add_field(
        name="Community",
        value="\n".join(
            [
                "`!process` open the menu",
                "`!process nvidia oa` update status",
                "`!process peers nvidia` find peers",
                "`!process nvidia` stats / notes",
            ]
        ),
        inline=False,
    )
    if is_admin:
        embed.add_field(
            name="Admin",
            value="\n".join(
                [
                    "`!runjobs` manual run",
                    "`!firecompany <company|id>` mark hot",
                    "`!synccompanies` refresh watchlist",
                ]
            ),
            inline=False,
        )
    embed.set_footer(text="This message will disappear shortly.")
    return embed


async def run_bot() -> None:
    config = load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger("bot")

    if not config.discord_token:
        raise RuntimeError("DISCORD_TOKEN is required")
    if not config.discord_channel_id:
        raise RuntimeError("DISCORD_CHANNEL_ID is required")

    database = Database(config.database_dsn)
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
        destination: Optional[Any] = None,
        reaction_user_id: Optional[int] = None,
    ) -> None:
        target = destination or ctx
        message = await target.send(embed=build_page(0))
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
                and (reaction_user_id is None or user.id == reaction_user_id)
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
        destination: Optional[Any] = None,
        reaction_user_id: Optional[int] = None,
    ) -> None:
        await _send_paginated_embed_pages(
            ctx,
            total_items=len(entries),
            page_size=page_size,
            destination=destination,
            reaction_user_id=reaction_user_id,
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

    async def _require_job_community_profile(
        ctx: commands.Context,
    ) -> Optional[Any]:
        profile = await database.get_job_community_profile(str(ctx.author.id))
        if profile:
            return profile
        await ctx.send(
            "Run `!process` first to opt in. Default visibility is `matched`, and you can change it with `!visibility`."
        )
        return None

    async def _community_user_label(ctx: commands.Context, user_id: str) -> str:
        try:
            numeric_user_id = int(user_id)
        except ValueError:
            return user_id

        if ctx.guild:
            member = ctx.guild.get_member(numeric_user_id)
            if member:
                return member.display_name
            with contextlib.suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
                member = await ctx.guild.fetch_member(numeric_user_id)
                return member.display_name

        user = bot.get_user(numeric_user_id)
        if user:
            return getattr(user, "display_name", user.name)
        with contextlib.suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
            user = await bot.fetch_user(numeric_user_id)
            return getattr(user, "display_name", user.name)
        return user_id

    async def _resolve_process_destination(
        ctx: commands.Context,
        *,
        profile: Optional[Any] = None,
        force_dm: bool = False,
    ) -> Optional[Any]:
        if not ctx.guild:
            return ctx
        if not force_dm and (not profile or profile["visibility"] == "open"):
            return ctx
        try:
            destination = ctx.author.dm_channel or await ctx.author.create_dm()
        except discord.HTTPException:
            with contextlib.suppress(discord.HTTPException):
                await ctx.message.delete()
            await ctx.send(
                "I couldn't DM you. Enable DMs from server members and try again.",
                delete_after=15,
            )
            return None
        with contextlib.suppress(discord.HTTPException):
            await ctx.message.delete()
        return destination

    async def _send_process_message(
        ctx: commands.Context,
        content: str,
        *,
        profile: Optional[Any] = None,
        force_dm: bool = False,
    ) -> None:
        destination = await _resolve_process_destination(ctx, profile=profile, force_dm=force_dm)
        if destination is None:
            return
        await destination.send(content)

    async def _handle_visibility(
        ctx: commands.Context,
        profile: Any,
        value: str,
    ) -> None:
        if not value.strip():
            await _send_process_message(
                ctx,
                f"Your visibility is `{format_job_visibility(profile['visibility'])}`. "
                "Use `!process open`, `!process matched`, or `!process private` to change it.",
                profile=profile,
            )
            return
        visibility = normalize_job_visibility(value)
        if not visibility:
            await _send_process_message(
                ctx,
                "Visibility must be one of `matched`, `open`, or `private`.",
                profile=profile,
            )
            return
        updated = await database.update_job_community_visibility(str(ctx.author.id), visibility)
        if not updated:
            await ctx.send("Run `!process` first to opt in.")
            return
        await _send_process_message(
            ctx,
            f"Visibility updated to `{format_job_visibility(updated['visibility'])}`.",
            profile=updated,
        )

    async def _handle_jobstatus(
        ctx: commands.Context,
        profile: Any,
        value: str,
    ) -> None:
        parsed = parse_jobstatus_input(value)
        if not parsed:
            await _send_process_message(ctx, jobstatus_usage_text(), profile=profile)
            return
        company_query, status = parsed
        company = await database.find_watchlist_company(company_query)
        if not company:
            await _send_process_message(
                ctx,
                "Company not found in watchlist. Use `!companies [query]` to search.",
                profile=profile,
            )
            return
        await database.upsert_job_application_status(
            user_id=str(ctx.author.id),
            company_id=int(company["id"]),
            status=status,
        )
        if status == "hide":
            await _send_process_message(
                ctx,
                f"Set your status for **{company['company']}** to `{format_job_tracking_status(status)}`. "
                "You will not appear in peer lookups for this company.",
                profile=profile,
            )
            return

        visibility = profile["visibility"]
        if visibility == "private":
            visibility_note = "Your profile is still `Private`, so others cannot discover you yet."
        elif visibility == "matched":
            visibility_note = "Matched peers can see you only if they also track this company."
        else:
            visibility_note = "Open peers can discover you from this company."
        await _send_process_message(
            ctx,
            f"Set your status for **{company['company']}** to `{format_job_tracking_status(status)}`. "
            f"{visibility_note}",
            profile=profile,
        )

    async def _handle_myjobs(
        ctx: commands.Context,
        profile: Any,
    ) -> None:
        rows = await database.list_job_application_statuses_for_user(
            str(ctx.author.id),
            limit=100,
        )
        if not rows:
            await _send_process_message(
                ctx,
                "You have not tracked any companies yet. Use `!process nvidia applied` to start.",
                profile=profile,
                force_dm=True,
            )
            return
        entries = [
            (
                f"**{row['company']}**\n"
                f"`{format_job_tracking_status(row['status'])}` • updated {_format_relative_timestamp(row['updated_at'])}"
                + _latest_process_note_text(row)
            )
            for row in rows
        ]
        destination = await _resolve_process_destination(ctx, profile=profile, force_dm=True)
        if destination is None:
            return
        await _send_paginated_entries(
            ctx,
            title="Your Process Tracker",
            entries=entries,
            page_size=MY_JOBS_PAGE_SIZE,
            description=f"Visibility: `{format_job_visibility(profile['visibility'])}`",
            color=discord.Color.dark_blue(),
            destination=destination,
            reaction_user_id=ctx.author.id,
        )

    async def _handle_jobpeers(
        ctx: commands.Context,
        profile: Any,
        query: str,
    ) -> None:
        company = await database.find_watchlist_company(query.strip())
        if not company:
            await _send_process_message(
                ctx,
                "Company not found in watchlist. Use `!companies [query]` to search.",
                profile=profile,
            )
            return
        own_status = await database.get_job_application_status(
            user_id=str(ctx.author.id),
            company_id=int(company["id"]),
        )
        include_matched = bool(own_status and own_status["status"] != "hide")
        peers = await database.list_job_peers(
            company_id=int(company["id"]),
            requester_user_id=str(ctx.author.id),
            include_matched=include_matched,
            limit=100,
        )
        if not peers:
            if include_matched:
                await _send_process_message(
                    ctx,
                    f"No opted-in peers are currently visible for **{company['company']}**.",
                    profile=profile,
                    force_dm=True,
                )
            else:
                await _send_process_message(
                    ctx,
                    f"No open peers are currently visible for **{company['company']}**. "
                    "Set your own status on that company to unlock `matched` peers too.",
                    profile=profile,
                    force_dm=True,
                )
            return
        labels: dict[str, str] = {}
        for row in peers:
            labels[row["user_id"]] = await _community_user_label(ctx, row["user_id"])
        entries = [
            (
                f"**{labels[row['user_id']]}**\n"
                f"`{format_job_tracking_status(row['status'])}` • "
                f"`{format_job_visibility(row['visibility'])}` • "
                f"updated {_format_relative_timestamp(row['updated_at'])}"
                + _latest_process_note_text(row)
            )
            for row in peers
        ]
        description = (
            f"Company: **{company['company']}**\n"
            + (
                "Showing `open` and `matched` peers because you also track this company."
                if include_matched
                else "Showing `open` peers only. Track this company yourself to unlock `matched` peers too."
            )
            + f"\n{JOB_COMMUNITY_RULES}"
        )
        destination = await _resolve_process_destination(ctx, profile=profile, force_dm=True)
        if destination is None:
            return
        await _send_paginated_entries(
            ctx,
            title="Process Peers",
            entries=entries,
            page_size=JOB_PEER_PAGE_SIZE,
            description=description,
            color=discord.Color.gold(),
            destination=destination,
            reaction_user_id=ctx.author.id,
        )

    async def _handle_jobprocess(
        ctx: commands.Context,
        profile: Any,
        value: str,
    ) -> None:
        parsed = parse_jobprocess_input(value)
        if not parsed:
            await _send_process_message(
                ctx,
                jobprocess_usage_text(),
                profile=profile,
                force_dm=True,
            )
            return
        company_query, note, notes_only = parsed
        company = await database.find_watchlist_company(company_query)
        if not company:
            await _send_process_message(
                ctx,
                "Company not found in watchlist. Use `!companies [query]` to search.",
                profile=profile,
                force_dm=True,
            )
            return
        own_status = await database.get_job_application_status(
            user_id=str(ctx.author.id),
            company_id=int(company["id"]),
        )

        if note is not None:
            cleaned_note = _clean_job_process_note(note)
            if not cleaned_note:
                await _send_process_message(
                    ctx,
                    "Notes cannot be empty.",
                    profile=profile,
                    force_dm=True,
                )
                return
            current_status = own_status["status"] if own_status else "save"
            await database.upsert_job_application_status(
                user_id=str(ctx.author.id),
                company_id=int(company["id"]),
                status=current_status,
            )
            await database.add_process_note(
                user_id=str(ctx.author.id),
                company_id=int(company["id"]),
                body=cleaned_note,
            )
            await _send_process_message(
                ctx,
                f"Saved your note for **{company['company']}** under `{format_job_tracking_status(current_status)}`.",
                profile=profile,
                force_dm=True,
            )
            return

        include_matched = bool(own_status and own_status["status"] != "hide")
        peers = await database.list_job_peers(
            company_id=int(company["id"]),
            requester_user_id=str(ctx.author.id),
            include_matched=include_matched,
            limit=100,
        )

        visible_rows: List[Any] = list(peers)
        if own_status and own_status["status"] != "hide":
            visible_rows = [own_status, *visible_rows]

        if not visible_rows:
            await _send_process_message(
                ctx,
                f"No visible process data is available for **{company['company']}** yet.",
                profile=profile,
                force_dm=True,
            )
            return

        count_summary = _job_process_counts_text(visible_rows)
        description_lines = [
            f"Company: **{company['company']}**",
            f"Visible statuses: {count_summary}",
            (
                "Scope: `open` + `matched` peers"
                if include_matched
                else "Scope: `open` peers only"
            ),
        ]
        if own_status and own_status["status"] != "hide":
            description_lines.append(
                f"Your status: `{format_job_tracking_status(own_status['status'])}`"
            )

        entry_rows = visible_rows
        if notes_only:
            entry_rows = await database.list_process_notes(
                company_id=int(company["id"]),
                requester_user_id=str(ctx.author.id),
                include_matched=include_matched,
                limit=100,
            )
            if not entry_rows:
                await _send_process_message(
                    ctx,
                    f"No visible notes are available for **{company['company']}** yet.",
                    profile=profile,
                    force_dm=True,
                )
                return

        labels: dict[str, str] = {}
        for row in entry_rows:
            if row["user_id"] == str(ctx.author.id):
                continue
            labels[row["user_id"]] = await _community_user_label(ctx, row["user_id"])
        entries = [
            (
                f"**{'You' if row['user_id'] == str(ctx.author.id) else labels[row['user_id']]}**\n"
                + (
                    f"`{format_job_tracking_status(row['status'])}` • noted {_format_relative_timestamp(row['created_at'])}\n"
                    f"Note: {row['body']}"
                    if notes_only
                    else f"`{format_job_tracking_status(row['status'])}` • updated {_format_relative_timestamp(row['updated_at'])}"
                    + _latest_process_note_text(row)
                )
            )
            for row in entry_rows
        ]
        destination = await _resolve_process_destination(ctx, profile=profile, force_dm=True)
        if destination is None:
            return
        await _send_paginated_entries(
            ctx,
            title="Job Process",
            entries=entries,
            page_size=JOB_PEER_PAGE_SIZE,
            description="\n".join(description_lines),
            color=discord.Color.orange(),
            destination=destination,
            reaction_user_id=ctx.author.id,
        )

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
        hot_company_names = await database.get_hot_watchlist_company_names()
        jobs = sort_recent_jobs(jobs, hot_company_names)
        await _send_paginated_embed_pages(
            ctx,
            total_items=len(jobs),
            page_size=RECENT_PAGE_SIZE,
            build_page=lambda page_index: build_recent_jobs_embed(
                jobs,
                page_index=page_index,
                page_size=RECENT_PAGE_SIZE,
                company_display_names=hot_company_names,
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
        await _send_paginated_entries(
            ctx,
            title="Command Help",
            entries=build_help_entries(),
            page_size=HELP_PAGE_SIZE,
            color=discord.Color.purple(),
        )

    @bot.command(name="help")
    async def help_command(ctx: commands.Context) -> None:
        await ctx.send(
            embed=build_quick_help_embed(is_admin=user_has_admin_privileges(ctx.author)),
            delete_after=45,
        )
        with contextlib.suppress(discord.HTTPException):
            await ctx.message.delete()

    @bot.command(name="helpjobs")
    async def helpjobs(ctx: commands.Context) -> None:
        await _send_helpjobs(ctx)

    @bot.command(name="process", aliases=["jobsaccept"])
    async def process_command(ctx: commands.Context, *, value: str = "") -> None:
        existing = await database.get_job_community_profile(str(ctx.author.id))
        profile = await database.accept_job_community_rules(
            str(ctx.author.id),
            default_visibility=JOB_VISIBILITY_DEFAULT,
        )
        action, payload = parse_process_command_input(value)
        if action == "help":
            await _send_process_message(
                ctx,
                build_process_help_message(
                    visibility=format_job_visibility(profile["visibility"]),
                    joined=not bool(existing),
                ),
                profile=profile,
            )
            return
        if action == "visibility":
            await _handle_visibility(ctx, profile, payload)
            return
        if action == "jobstatus":
            await _handle_jobstatus(ctx, profile, payload)
            return
        if action == "myjobs":
            await _handle_myjobs(ctx, profile)
            return
        if action == "jobpeers":
            await _handle_jobpeers(ctx, profile, payload)
            return
        await _handle_jobprocess(ctx, profile, payload)

    @bot.command(name="visibility")
    async def visibility_command(ctx: commands.Context, *, value: str = "") -> None:
        profile = await _require_job_community_profile(ctx)
        if not profile:
            return
        await _handle_visibility(ctx, profile, value)

    @bot.command(name="jobstatus")
    async def jobstatus_command(ctx: commands.Context, *, value: str) -> None:
        profile = await _require_job_community_profile(ctx)
        if not profile:
            return
        await _handle_jobstatus(ctx, profile, value)

    @bot.command(name="myjobs")
    async def myjobs_command(ctx: commands.Context) -> None:
        profile = await _require_job_community_profile(ctx)
        if not profile:
            return
        await _handle_myjobs(ctx, profile)

    @bot.command(name="jobpeers")
    async def jobpeers_command(ctx: commands.Context, *, query: str) -> None:
        profile = await _require_job_community_profile(ctx)
        if not profile:
            return
        await _handle_jobpeers(ctx, profile, query)

    @bot.command(name="jobprocess", aliases=["jobstat"])
    async def jobprocess_command(ctx: commands.Context, *, value: str) -> None:
        profile = await _require_job_community_profile(ctx)
        if not profile:
            return
        await _handle_jobprocess(ctx, profile, value)

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

    @bot.command(name="firecompany")
    @restricted_command
    async def firecompany(ctx: commands.Context, *, query: str) -> None:
        try:
            company = await watchlist_service.mark_company_hot(query)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(f"Marked {company['company']} as a hot company.")

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
