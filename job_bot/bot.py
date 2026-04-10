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

from job_bot.config import (
    RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY,
    RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
    apply_runtime_channel_overrides,
    configure_logging,
    is_new_grad_collection_enabled,
    load_config,
)
from job_bot.database import Database
from job_bot.process_tracking import (
    PROCESS_NOTE_RATE_LIMIT_MAX,
    PROCESS_NOTE_RATE_LIMIT_WINDOW_SECONDS,
    PROCESS_REPORT_RATE_LIMIT_MAX,
    PROCESS_REPORT_RATE_LIMIT_WINDOW_SECONDS,
    PROCESS_STATUS_RATE_LIMIT_MAX,
    PROCESS_STATUS_RATE_LIMIT_WINDOW_SECONDS,
    PROCESS_VISIBILITY_DEFAULT,
    build_process_next_action,
    clean_process_note,
    format_process_status,
    format_process_visibility,
    infer_process_track,
    normalize_process_status,
    normalize_process_track,
    normalize_role_title,
    normalize_process_visibility,
    normalize_season_term,
    parse_process_note_command,
    parse_process_status_command,
    process_scope_score,
    validate_process_note,
)
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
JOB_PEER_PAGE_SIZE = 10
MY_JOBS_PAGE_SIZE = 10
CHANNEL_STATUS_DISABLED = "disabled"

JOB_VISIBILITY_DEFAULT = PROCESS_VISIBILITY_DEFAULT
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
    async def predicate(ctx: commands.Context) -> bool:
        if user_has_admin_privileges(ctx.author):
            return True
        if ctx.guild is not None:
            raise commands.CheckFailure("This command requires Discord administrator privileges.")

        author_id = getattr(ctx.author, "id", None)
        if author_id is None:
            raise commands.CheckFailure("This command requires Discord administrator privileges.")

        for guild in ctx.bot.guilds:
            member = guild.get_member(author_id)
            if member is None:
                with contextlib.suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                    member = await guild.fetch_member(author_id)
            if member and user_has_admin_privileges(member):
                return True

        raise commands.CheckFailure("This command requires Discord administrator privileges.")

    return commands.check(predicate)


def parse_channel_command_value(value: str, *, allow_off: bool = False) -> Optional[int]:
    token = value.strip()
    if allow_off and token.lower() == "off":
        return None
    if not token.isdigit():
        expected = "`off` or a numeric channel ID" if allow_off else "a numeric channel ID"
        raise ValueError(f"Use {expected}.")
    return int(token)


def validate_configurable_channel(channel: Any, bot_member: Any) -> Optional[str]:
    if getattr(channel, "guild", None) is None or not hasattr(channel, "permissions_for") or not hasattr(channel, "send"):
        return "Channel must be a text channel I can post in."

    permissions = channel.permissions_for(bot_member)
    if not getattr(permissions, "view_channel", False):
        return "I can't view that channel."

    channel_type = getattr(channel, "type", None)
    is_thread = channel_type in {
        discord.ChannelType.news_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.public_thread,
    }
    can_send = (
        getattr(permissions, "send_messages_in_threads", False)
        if is_thread
        else getattr(permissions, "send_messages", False)
    )
    if not can_send:
        return "I can't send messages to that channel."
    if not getattr(permissions, "embed_links", False):
        return "I can't embed links in that channel."
    return None


def build_channel_configuration_message(
    *,
    config: Any,
    runtime_config: dict[str, Any],
    startup_channel_defaults: dict[str, Optional[int]],
    startup_new_grad_collection_enabled: bool,
) -> str:
    internship_source = (
        "runtime override" if RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY in runtime_config else "env default"
    )
    new_grad_source = (
        "runtime override" if RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY in runtime_config else "env default"
    )
    internship_default = startup_channel_defaults.get(RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY)
    new_grad_default = startup_channel_defaults.get(RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY)

    return "\n".join(
        [
            "Channel routing:",
            (
                f"Internship: `{config.discord_channel_id}` "
                f"({internship_source}; startup default `{internship_default}`)"
            ),
            (
                f"New grad: "
                f"`{config.discord_new_grad_channel_id}`"
                if config.discord_new_grad_channel_id is not None
                else f"New grad: `{CHANNEL_STATUS_DISABLED}`"
            )
            + (
                f" ({new_grad_source}; startup default `{new_grad_default}`)"
                if new_grad_default is not None
                else f" ({new_grad_source}; startup default `{CHANNEL_STATUS_DISABLED}`)"
            ),
            (
                "New-grad collection at startup: "
                f"`{'enabled' if startup_new_grad_collection_enabled else 'disabled'}`"
            ),
        ]
    )


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
    return normalize_process_visibility(value)


def format_job_visibility(visibility: str) -> str:
    return format_process_visibility(visibility)


def normalize_job_tracking_status(value: str) -> Optional[str]:
    return normalize_process_status(value)


def parse_jobstatus_input(value: str) -> Optional[tuple[str, str]]:
    raw = value.strip()
    if not raw:
        return None
    tokens = raw.split()
    if len(tokens) == 2 and tokens[0].isdigit():
        status = normalize_job_tracking_status(tokens[1])
        if status:
            return tokens[0], status
    return None


def parse_process_command_input(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        return "dashboard", ""

    if normalize_job_visibility(raw):
        return "visibility", raw

    head, _, tail = raw.partition(" ")
    action = head.lower()
    action_aliases = {
        "help": "help",
        "menu": "help",
        "me": "dashboard",
        "my": "dashboard",
        "mine": "dashboard",
        "peer": "jobpeers",
        "peers": "jobpeers",
        "visibility": "visibility",
        "vis": "visibility",
        "status": "jobstatus",
        "set": "jobstatus",
        "track": "jobstatus",
        "view": "jobprocess",
        "stat": "jobprocess",
        "stats": "jobprocess",
        "note": "processnote",
        "notes": "processnotes",
        "comment": "processnote",
        "comments": "processnotes",
        "report": "processreport",
        "deletenote": "processdeletenote",
        "delete": "processdeletenote",
        "company": "processcompany",
    }
    if action in action_aliases:
        resolved = action_aliases[action]
        return resolved, tail

    if parse_jobstatus_input(raw):
        return "jobstatus", raw
    return "processcompany", raw


def format_job_tracking_status(status: str) -> str:
    return format_process_status(status)


def job_status_sort_key(status: str) -> int:
    return JOB_STATUS_ORDER.get(status, 99)


def jobstatus_usage_text() -> str:
    return (
        "Use `!process set Company | Role Title | Season | Status` "
        "or `!process set Company | Role Title | Track | Season | Status`.\n"
        "Quick updates also support `!process status <scope_id> status`."
    )


def build_process_help_message(*, visibility: str, joined: bool) -> str:
    lines = [
        "You're in the job-process community." if joined else "Job process commands",
        f"`Visibility:` {visibility}",
        "Process replies stay in DM by default. Visibility only controls peer discovery.",
        "",
        "`Use:`",
        "• `!process` or `!process me` view your active tracked roles",
        "• `!process set Stripe | Software Engineer Intern | Summer 2027 | applied` track a role",
        "• `!process company Stripe` list role scopes for that company",
        "• `!process status 12 interview` update a tracked scope",
        "• `!process view 12` view one role scope",
        "• `!process peers 12` find peers for one role scope",
        "• `!process note 12 | recruiter said 2 rounds` save a note",
        "• `!process notes 12` read visible notes",
        "• `!process report 34 leaked oa details` report a note",
        "• `!process deletenote 34` delete your own note",
        "• `!process open` or `!process visibility open` change discovery",
        "",
        JOB_COMMUNITY_RULES,
    ]
    return "\n".join(lines)


def _clean_job_process_note(note: str) -> str:
    return clean_process_note(note)


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


def _format_process_scope_line(scope: Any) -> str:
    return (
        f"`#{scope['id']}` **{scope['role_title']}**\n"
        f"`{scope['track']}` • `{scope['season_term']}` • **{scope['company']}**"
    )


def _format_process_scope_title(scope: Any) -> str:
    return f"{scope['company']} • {scope['role_title']} • {scope['season_term']}"


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


def build_help_page_definitions() -> List[tuple[str, List[tuple[str, List[str]]]]]:
    return [
        (
            "Browse",
            [
                (
                    "Browse",
                    [
                        "`!ping` - health check",
                        "`!help` - show the full grouped command guide",
                        "`!helpjobs` - compatibility alias for the same help guide",
                        "`!recent` - show the most recent jobs",
                    ],
                ),
                (
                    "Companies",
                    [
                        "`!companies [query]` - list watchlist companies",
                        "`!companyinfo <company|id>` - show watchlist details",
                        "`!requestcompany <company> | <careers_url> | [homepage_url]` - request a company",
                    ],
                ),
                (
                    "Requests",
                    [
                        "`!companyrequests [pending|approved|denied|all]` - request queue [admin only]",
                        "`!approvecompany <request_id>` - approve a company request [admin only]",
                        "`!denycompany <request_id> [reason]` - deny a company request [admin only]",
                    ],
                ),
            ],
        ),
        (
            "Process Tracking",
            [
                (
                    "Process Tracking",
                    [
                        "`!process` - DM-first dashboard for your active tracked roles",
                        "`!process set <company> | <role> | <season> | <status>` - track a role scope",
                        "`!process set <company> | <role> | <track> | <season> | <status>` - set track explicitly",
                        "`!process company <company>` - list role scopes for one company",
                        "`!process view <scope_id>` - view one role scope",
                        "`!process status <scope_id> <status>` - update one role scope",
                    ],
                ),
                (
                    "Process Notes And Peers",
                    [
                        "`!process peers <scope_id>` - find peers for one role scope",
                        "`!process notes <scope_id>` - read visible notes",
                        "`!process note <scope_id> | <note>` - save a note",
                        "`!process report <note_id> [reason]` - report leaked/private content",
                        "`!process deletenote <note_id> [reason]` - delete your own note",
                        "`!process open|matched|private` - change peer discovery visibility",
                    ],
                ),
                (
                    "Aliases / Compatibility",
                    [
                        "`!visibility [open|matched|private]` - alias for visibility changes",
                        "`!jobstatus <scope_id> <status>` - alias for role status updates",
                        "`!myjobs` - alias for your process dashboard",
                        "`!jobpeers <scope_id>` - alias for role peers",
                        "`!jobprocess <scope_id>` - alias for role view",
                    ],
                ),
            ],
        ),
        (
            "Admin",
            [
                (
                    "Admin",
                    [
                        "`!runjobs` - trigger a manual discovery run [admin only]",
                        "`!stats` - show stored job stats [admin only]",
                        "`!sources` - list configured sources [admin only]",
                        "`!dedupecheck <url or text>` - test duplicate detection [admin only]",
                        "`!season [value]` - show season filter configuration [admin only]",
                        "`!summary [daily|weekly]` - show recent summary stats [admin only]",
                        "`!showchannels` - show effective posting channel routing [admin only]",
                        "`!setinternchannel <channel_id>` - update the internship posting channel [admin only]",
                        "`!setnewgradchannel <channel_id|off>` - update or disable the new-grad posting channel [admin only]",
                        "`!firecompany <company|id>` - mark a watchlist company as hot [admin only]",
                        "`!synccompanies` - reimport the company watchlist seed [admin only]",
                    ],
                )
            ],
        ),
    ]


def build_help_embed(page_index: int) -> discord.Embed:
    pages = build_help_page_definitions()
    total_pages = len(pages)
    safe_page_index = min(max(page_index, 0), total_pages - 1)
    page_title, sections = pages[safe_page_index]

    embed = discord.Embed(
        title=f"Help • {page_title}",
        description=(
            "Process commands reply in DM by default.\n"
            "Admin commands require Discord administrator privileges."
        ),
        color=discord.Color.purple(),
    )
    for section_title, entries in sections:
        embed.add_field(name=section_title, value="\n".join(entries), inline=False)
    embed.set_footer(
        text=f"Page {safe_page_index + 1}/{total_pages} • Use reactions to browse"
    )
    return embed


async def run_bot() -> None:
    config = load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger("bot")
    startup_channel_defaults = {
        RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY: config.discord_channel_id,
        RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY: config.discord_new_grad_channel_id,
    }
    startup_new_grad_collection_enabled = is_new_grad_collection_enabled(config)

    if not config.discord_token:
        raise RuntimeError("DISCORD_TOKEN is required")
    if not config.discord_channel_id:
        raise RuntimeError("DISCORD_CHANNEL_ID is required")

    database = Database(config.database_dsn)
    await database.initialize()
    runtime_config_rows = await database.list_runtime_config()
    apply_runtime_channel_overrides(
        config,
        {row["key"]: row for row in runtime_config_rows},
    )
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

    async def _runtime_config_by_key() -> dict[str, Any]:
        return {row["key"]: row for row in await database.list_runtime_config()}

    async def _resolve_configurable_channel(channel_id: int) -> Any:
        channel = bot.get_channel(channel_id)
        if channel is None:
            with contextlib.suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                channel = await bot.fetch_channel(channel_id)
        if channel is None:
            raise ValueError("Channel not found.")

        guild = getattr(channel, "guild", None)
        if guild is None:
            raise ValueError("Channel must belong to a server.")

        bot_member = guild.me
        if bot_member is None and bot.user is not None:
            bot_member = guild.get_member(bot.user.id)
        if bot_member is None and bot.user is not None:
            with contextlib.suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                bot_member = await guild.fetch_member(bot.user.id)
        if bot_member is None:
            raise ValueError("I couldn't verify my permissions for that channel.")

        permission_error = validate_configurable_channel(channel, bot_member)
        if permission_error:
            raise ValueError(permission_error)
        return channel

    async def _set_channel_override(
        *,
        key: str,
        channel_id: Optional[int],
        updated_by_user_id: str,
    ) -> dict[str, Any]:
        row = await database.set_runtime_config_value(
            key=key,
            value=str(channel_id) if channel_id is not None else None,
            updated_by_user_id=updated_by_user_id,
        )
        apply_runtime_channel_overrides(config, {row["key"]: row})
        return row

    async def _send_channel_configuration(ctx: commands.Context) -> None:
        runtime_config = await _runtime_config_by_key()
        await ctx.send(
            build_channel_configuration_message(
                config=config,
                runtime_config=runtime_config,
                startup_channel_defaults=startup_channel_defaults,
                startup_new_grad_collection_enabled=startup_new_grad_collection_enabled,
            )
        )

    async def _refresh_branding_posts(normalized_companies: Sequence[str]) -> int:
        jobs = await database.get_posted_canonical_jobs_for_companies(normalized_companies)
        if not jobs:
            return 0
        refreshed = await publisher.refresh_jobs(jobs)
        return len(refreshed)

    @bot.event
    async def on_ready() -> None:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Maintained by KH",
            ),
        )
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
            "Run `!process` first to opt in. Default visibility is `private`, and you can change it with `!visibility`."
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
        # Process updates are treated as sensitive community data. In guilds we
        # always reply over DM and delete the invoking message when possible so
        # status updates or notes do not leak into public channels by default.
        if not ctx.guild:
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

    async def _resolve_process_scope_for_set(
        *,
        company_query: str,
        role_title: str,
        season_term: str,
        explicit_track: Optional[str],
    ) -> tuple[Optional[Any], Optional[Any], Optional[str]]:
        company = await database.find_watchlist_company(company_query)
        if not company:
            return None, None, "Company not found in watchlist. Use `!companies [query]` to search."

        normalized_company = company["normalized_company"]
        normalized_role = normalize_role_title(role_title)
        canonical_candidates = await database.search_text(
            f"{company['company']} {role_title} {season_term}",
            limit=25,
        )
        canonical_candidates = [
            candidate
            for candidate in canonical_candidates
            if candidate.normalized_company == normalized_company
        ]

        best_candidate = None
        best_score = 0.0
        for candidate in canonical_candidates:
            if explicit_track and candidate.track != explicit_track:
                continue
            score = process_scope_score(
                role_title,
                candidate.title,
                season_term,
                candidate.season_term or "",
            )
            if score > best_score:
                best_candidate = candidate
                best_score = score

        canonical_track = best_candidate.track if best_candidate else None
        resolved_track = explicit_track or infer_process_track(
            role_title=role_title,
            canonical_track=canonical_track,
        )
        if resolved_track is None:
            return None, None, (
                "I could not infer whether that role is `internship` or `new_grad`. "
                "Use `!process set Company | Role | Track | Season | Status`."
            )

        resolved_role_title = best_candidate.title if best_candidate and best_score >= 85 else role_title
        resolved_season_term = (
            best_candidate.season_term
            if best_candidate and best_candidate.season_term and best_score >= 85
            else season_term
        )
        scope = await database.upsert_process_role_scope(
            company_id=int(company["id"]),
            canonical_job_id=best_candidate.id if best_candidate and best_score >= 85 else None,
            company=company["company"],
            normalized_company=normalized_company,
            role_title=resolved_role_title,
            normalized_role_title=normalize_role_title(resolved_role_title),
            track=resolved_track,
            season_term=resolved_season_term,
            canonical_job_url=best_candidate.canonical_url if best_candidate and best_score >= 85 else "",
            scope_origin="canonical" if best_candidate and best_score >= 85 else "manual",
        )
        return company, scope, None

    async def _rate_limit_process_action(
        *,
        user_id: str,
        event_type: str,
        max_events: int,
        window_seconds: int,
        failure_message: str,
        ctx: commands.Context,
        profile: Any,
    ) -> bool:
        recent_count = await database.count_recent_process_activity(
            user_id=user_id,
            event_type=event_type,
            since_seconds=window_seconds,
        )
        if recent_count >= max_events:
            await _send_process_message(
                ctx,
                failure_message,
                profile=profile,
                force_dm=True,
            )
            return False
        return True

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
                force_dm=True,
            )
            return
        visibility = normalize_job_visibility(value)
        if not visibility:
            await _send_process_message(
                ctx,
                "Visibility must be one of `matched`, `open`, or `private`.",
                profile=profile,
                force_dm=True,
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
            force_dm=True,
        )

    async def _handle_jobstatus(
        ctx: commands.Context,
        profile: Any,
        value: str,
    ) -> None:
        user_id = str(ctx.author.id)
        if not await _rate_limit_process_action(
            user_id=user_id,
            event_type="status_update",
            max_events=PROCESS_STATUS_RATE_LIMIT_MAX,
            window_seconds=PROCESS_STATUS_RATE_LIMIT_WINDOW_SECONDS,
            failure_message="Too many process status updates. Wait a few minutes and try again.",
            ctx=ctx,
            profile=profile,
        ):
            return

        quick_update = parse_jobstatus_input(value)
        if quick_update:
            scope_id = int(quick_update[0])
            status = quick_update[1]
            scope = await database.get_process_role_scope(scope_id)
            if not scope:
                await _send_process_message(
                    ctx,
                    "Role scope not found. Use `!process company Company Name` to list scope IDs.",
                    profile=profile,
                    force_dm=True,
                )
                return
        else:
            parsed = parse_process_status_command(value)
            if not parsed:
                await _send_process_message(ctx, jobstatus_usage_text(), profile=profile, force_dm=True)
                return
            company_query, role_title, season_term, status, explicit_track = parsed
            _, scope, error_message = await _resolve_process_scope_for_set(
                company_query=company_query,
                role_title=role_title,
                season_term=season_term,
                explicit_track=explicit_track,
            )
            if error_message:
                await _send_process_message(ctx, error_message, profile=profile, force_dm=True)
                return
            if scope is None:
                await _send_process_message(
                    ctx,
                    "I couldn't resolve that role scope.",
                    profile=profile,
                    force_dm=True,
                )
                return
            scope_id = int(scope["id"])

        status_row = await database.upsert_process_role_status(
            user_id=user_id,
            scope_id=scope_id,
            status=status,
        )
        await database.record_process_activity(
            user_id=user_id,
            event_type="status_update",
            scope_id=scope_id,
            metadata={"status": status},
        )
        visibility = format_job_visibility(profile["visibility"])
        next_action = build_process_next_action(status, status_row["updated_at"])
        await _send_process_message(
            ctx,
            (
                f"Tracked **{status_row['company']}** • **{status_row['role_title']}** "
                f"(`#{scope_id}` • `{status_row['track']}` • `{status_row['season_term']}`)\n"
                f"Status: `{format_job_tracking_status(status)}`\n"
                f"Visibility: `{visibility}`\n"
                f"Next: {next_action}"
            ),
            profile=profile,
            force_dm=True,
        )

    async def _handle_myjobs(
        ctx: commands.Context,
        profile: Any,
        *,
        joined_recently: bool = False,
    ) -> None:
        rows = await database.list_process_role_statuses_for_user(
            str(ctx.author.id),
            active_only=True,
            limit=100,
        )
        if not rows:
            await _send_process_message(
                ctx,
                build_process_help_message(
                    visibility=format_job_visibility(profile["visibility"]),
                    joined=joined_recently,
                ),
                profile=profile,
                force_dm=True,
            )
            return
        entries = [
            (
                f"`#{row['scope_id']}` **{row['company']}** • **{row['role_title']}**\n"
                f"`{row['track']}` • `{row['season_term']}` • "
                f"`{format_job_tracking_status(row['status'])}` • updated {_format_relative_timestamp(row['updated_at'])}\n"
                f"Next: {build_process_next_action(row['status'], row['updated_at'])}"
                + _latest_process_note_text(row)
            )
            for row in rows
        ]
        destination = await _resolve_process_destination(ctx, profile=profile, force_dm=True)
        if destination is None:
            return
        await _send_paginated_entries(
            ctx,
            title="Your Active Process Roles",
            entries=entries,
            page_size=MY_JOBS_PAGE_SIZE,
            description=(
                f"Visibility: `{format_job_visibility(profile['visibility'])}`\n"
                "Track by role, not just company. Use the scope IDs here for status, notes, and peers."
            ),
            color=discord.Color.dark_blue(),
            destination=destination,
            reaction_user_id=ctx.author.id,
        )

    async def _handle_process_company(
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
                force_dm=True,
            )
            return
        scopes = await database.list_process_role_scopes_for_company(
            company_id=int(company["id"]),
            limit=100,
        )
        if not scopes:
            await _send_process_message(
                ctx,
                f"No process role scopes exist for **{company['company']}** yet. "
                "Use `!process set Company | Role | Season | Status` to create one.",
                profile=profile,
                force_dm=True,
            )
            return

        entries = []
        for scope in scopes:
            own_status = await database.get_process_role_status(
                user_id=str(ctx.author.id),
                scope_id=int(scope["id"]),
            )
            include_matched = bool(own_status and own_status["status"] != "hide")
            peers = await database.list_process_role_peers(
                scope_id=int(scope["id"]),
                requester_user_id=str(ctx.author.id),
                include_matched=include_matched,
                limit=100,
            )
            visible_rows: List[Any] = list(peers)
            if own_status and own_status["status"] != "hide":
                visible_rows = [own_status, *visible_rows]
            summary = _job_process_counts_text(visible_rows)
            own_status_text = (
                f"Your status: `{format_job_tracking_status(own_status['status'])}` • "
                f"{build_process_next_action(own_status['status'], own_status['updated_at'])}"
                if own_status
                else "Your status: not tracked yet."
            )
            entries.append(
                f"{_format_process_scope_line(scope)}\n"
                f"Visible community: {summary}\n"
                f"{own_status_text}"
            )

        destination = await _resolve_process_destination(ctx, profile=profile, force_dm=True)
        if destination is None:
            return
        await _send_paginated_entries(
            ctx,
            title=f"Process Roles • {company['company']}",
            entries=entries,
            page_size=JOB_PEER_PAGE_SIZE,
            description="Use a scope ID for `!process view`, `!process note`, `!process status`, or `!process peers`.",
            color=discord.Color.orange(),
            destination=destination,
            reaction_user_id=ctx.author.id,
        )

    async def _handle_jobpeers(
        ctx: commands.Context,
        profile: Any,
        query: str,
    ) -> None:
        if not query.strip().isdigit():
            await _send_process_message(
                ctx,
                "Use `!process peers <scope_id>`.",
                profile=profile,
                force_dm=True,
            )
            return
        scope = await database.get_process_role_scope(int(query.strip()))
        if not scope:
            await _send_process_message(
                ctx,
                "Role scope not found. Use `!process company Company Name` to list scope IDs.",
                profile=profile,
                force_dm=True,
            )
            return
        own_status = await database.get_process_role_status(
            user_id=str(ctx.author.id),
            scope_id=int(scope["id"]),
        )
        include_matched = bool(own_status and own_status["status"] != "hide")
        peers = await database.list_process_role_peers(
            scope_id=int(scope["id"]),
            requester_user_id=str(ctx.author.id),
            include_matched=include_matched,
            limit=100,
        )
        if not peers:
            await _send_process_message(
                ctx,
                f"No visible peers are currently available for **{scope['role_title']}** (`#{scope['id']}`).",
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
            f"{_format_process_scope_title(scope)}\n"
            + (
                "Showing `open` and `matched` peers because you also track this role."
                if include_matched
                else "Showing `open` peers only. Track this role yourself to unlock `matched` peers too."
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
        if not value.strip().isdigit():
            await _send_process_message(
                ctx,
                "Use `!process view <scope_id>`.",
                profile=profile,
                force_dm=True,
            )
            return
        scope = await database.get_process_role_scope(int(value.strip()))
        if not scope:
            await _send_process_message(
                ctx,
                "Role scope not found.",
                profile=profile,
                force_dm=True,
            )
            return
        own_status = await database.get_process_role_status(
            user_id=str(ctx.author.id),
            scope_id=int(scope["id"]),
        )
        include_matched = bool(own_status and own_status["status"] != "hide")
        peers = await database.list_process_role_peers(
            scope_id=int(scope["id"]),
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
                f"No visible process data is available for **{scope['role_title']}** (`#{scope['id']}`) yet.",
                profile=profile,
                force_dm=True,
            )
            return
        count_summary = _job_process_counts_text(visible_rows)
        description_lines = [
            _format_process_scope_title(scope),
            f"Visible statuses: {count_summary}",
        ]
        if own_status:
            description_lines.append(
                f"Your status: `{format_job_tracking_status(own_status['status'])}` • {build_process_next_action(own_status['status'], own_status['updated_at'])}"
            )
        labels: dict[str, str] = {}
        for row in visible_rows:
            if row["user_id"] == str(ctx.author.id):
                continue
            labels[row["user_id"]] = await _community_user_label(ctx, row["user_id"])
        entries = [
            (
                f"**{'You' if row['user_id'] == str(ctx.author.id) else labels[row['user_id']]}**\n"
                f"`{format_job_tracking_status(row['status'])}` • updated {_format_relative_timestamp(row['updated_at'])}"
                + _latest_process_note_text(row)
            )
            for row in visible_rows
        ]
        destination = await _resolve_process_destination(ctx, profile=profile, force_dm=True)
        if destination is None:
            return
        await _send_paginated_entries(
            ctx,
            title="Role Process View",
            entries=entries,
            page_size=JOB_PEER_PAGE_SIZE,
            description="\n".join(description_lines),
            color=discord.Color.orange(),
            destination=destination,
            reaction_user_id=ctx.author.id,
        )

    async def _handle_process_notes(
        ctx: commands.Context,
        profile: Any,
        value: str,
    ) -> None:
        if not value.strip().isdigit():
            await _send_process_message(
                ctx,
                "Use `!process notes <scope_id>`.",
                profile=profile,
                force_dm=True,
            )
            return
        scope = await database.get_process_role_scope(int(value.strip()))
        if not scope:
            await _send_process_message(ctx, "Role scope not found.", profile=profile, force_dm=True)
            return
        own_status = await database.get_process_role_status(
            user_id=str(ctx.author.id),
            scope_id=int(scope["id"]),
        )
        include_matched = bool(own_status and own_status["status"] != "hide")
        rows = await database.list_process_role_notes(
            scope_id=int(scope["id"]),
            requester_user_id=str(ctx.author.id),
            include_matched=include_matched,
            limit=100,
        )
        if not rows:
            await _send_process_message(
                ctx,
                f"No visible notes are available for **{scope['role_title']}** (`#{scope['id']}`) yet.",
                profile=profile,
                force_dm=True,
            )
            return
        labels: dict[str, str] = {}
        for row in rows:
            if row["user_id"] == str(ctx.author.id):
                continue
            labels[row["user_id"]] = await _community_user_label(ctx, row["user_id"])
        entries = [
            (
                f"`note #{row['id']}` **{'You' if row['user_id'] == str(ctx.author.id) else labels[row['user_id']]}**\n"
                f"`{format_job_tracking_status(row['status'])}` • noted {_format_relative_timestamp(row['created_at'])}\n"
                f"{row['body']}"
            )
            for row in rows
        ]
        destination = await _resolve_process_destination(ctx, profile=profile, force_dm=True)
        if destination is None:
            return
        await _send_paginated_entries(
            ctx,
            title="Role Notes",
            entries=entries,
            page_size=JOB_PEER_PAGE_SIZE,
            description=_format_process_scope_title(scope),
            color=discord.Color.teal(),
            destination=destination,
            reaction_user_id=ctx.author.id,
        )

    async def _handle_process_note(
        ctx: commands.Context,
        profile: Any,
        value: str,
    ) -> None:
        parsed = parse_process_note_command(value)
        if not parsed:
            await _send_process_message(
                ctx,
                "Use `!process note <scope_id> | <note>`.",
                profile=profile,
                force_dm=True,
            )
            return
        if not await _rate_limit_process_action(
            user_id=str(ctx.author.id),
            event_type="note_create",
            max_events=PROCESS_NOTE_RATE_LIMIT_MAX,
            window_seconds=PROCESS_NOTE_RATE_LIMIT_WINDOW_SECONDS,
            failure_message="Too many notes too quickly. Wait a bit before posting another note.",
            ctx=ctx,
            profile=profile,
        ):
            return
        scope_raw, note_text = parsed
        if not scope_raw.isdigit():
            await _send_process_message(
                ctx,
                "Use a numeric scope ID. Get one from `!process` or `!process company Company Name`.",
                profile=profile,
                force_dm=True,
            )
            return
        scope = await database.get_process_role_scope(int(scope_raw))
        if not scope:
            await _send_process_message(ctx, "Role scope not found.", profile=profile, force_dm=True)
            return
        cleaned_note = _clean_job_process_note(note_text)
        validation_error = validate_process_note(cleaned_note)
        if validation_error:
            await _send_process_message(ctx, validation_error, profile=profile, force_dm=True)
            return
        status_row = await database.get_process_role_status(
            user_id=str(ctx.author.id),
            scope_id=int(scope["id"]),
        )
        if not status_row:
            status_row = await database.upsert_process_role_status(
                user_id=str(ctx.author.id),
                scope_id=int(scope["id"]),
                status="save",
            )
        note = await database.add_process_role_note(
            status_id=int(status_row["id"]),
            scope_id=int(scope["id"]),
            user_id=str(ctx.author.id),
            body=cleaned_note,
        )
        await database.record_process_activity(
            user_id=str(ctx.author.id),
            event_type="note_create",
            scope_id=int(scope["id"]),
            metadata={"note_id": note["id"]},
        )
        await _send_process_message(
            ctx,
            (
                f"Saved note `#{note['id']}` for **{scope['company']}** • **{scope['role_title']}** "
                f"(`#{scope['id']}` • `{scope['season_term']}`)."
            ),
            profile=profile,
            force_dm=True,
        )

    async def _handle_process_report(
        ctx: commands.Context,
        profile: Any,
        value: str,
    ) -> None:
        if not await _rate_limit_process_action(
            user_id=str(ctx.author.id),
            event_type="note_report",
            max_events=PROCESS_REPORT_RATE_LIMIT_MAX,
            window_seconds=PROCESS_REPORT_RATE_LIMIT_WINDOW_SECONDS,
            failure_message="Too many note reports too quickly. Wait a bit and try again.",
            ctx=ctx,
            profile=profile,
        ):
            return
        raw = value.strip()
        if not raw:
            await _send_process_message(
                ctx,
                "Use `!process report <note_id> [reason]`.",
                profile=profile,
                force_dm=True,
            )
            return
        note_id_token, _, reason = raw.partition(" ")
        if not note_id_token.isdigit():
            await _send_process_message(
                ctx,
                "Use `!process report <note_id> [reason]`.",
                profile=profile,
                force_dm=True,
            )
            return
        note = await database.get_process_role_note(int(note_id_token))
        if not note:
            await _send_process_message(ctx, "Note not found.", profile=profile, force_dm=True)
            return
        if note["user_id"] == str(ctx.author.id):
            await _send_process_message(
                ctx,
                "Use `!process deletenote <note_id>` to remove your own note.",
                profile=profile,
                force_dm=True,
            )
            return
        updated_note = await database.report_process_role_note(
            note_id=int(note_id_token),
            reporter_user_id=str(ctx.author.id),
            reason=(reason or "reported"),
            auto_hide_threshold=1 if user_has_admin_privileges(ctx.author) else 2,
        )
        await database.record_process_activity(
            user_id=str(ctx.author.id),
            event_type="note_report",
            scope_id=int(note["scope_id"]),
            metadata={"note_id": int(note_id_token)},
        )
        if updated_note and updated_note["moderation_status"] == "hidden":
            message = f"Reported note `#{note_id_token}`. It is now hidden pending review."
        else:
            message = f"Reported note `#{note_id_token}`."
        await _send_process_message(ctx, message, profile=profile, force_dm=True)

    async def _handle_process_delete_note(
        ctx: commands.Context,
        profile: Any,
        value: str,
    ) -> None:
        raw = value.strip()
        if not raw:
            await _send_process_message(
                ctx,
                "Use `!process deletenote <note_id> [reason]`.",
                profile=profile,
                force_dm=True,
            )
            return
        note_id_token, _, reason = raw.partition(" ")
        if not note_id_token.isdigit():
            await _send_process_message(
                ctx,
                "Use `!process deletenote <note_id> [reason]`.",
                profile=profile,
                force_dm=True,
            )
            return
        deleted_note = await database.delete_process_role_note(
            note_id=int(note_id_token),
            actor_user_id=str(ctx.author.id),
            allow_moderator_delete=user_has_admin_privileges(ctx.author),
            reason=reason,
        )
        if not deleted_note:
            await _send_process_message(
                ctx,
                "You can only delete your own notes unless you are a server administrator.",
                profile=profile,
                force_dm=True,
            )
            return
        await _send_process_message(
            ctx,
            f"Deleted note `#{note_id_token}`.",
            profile=profile,
            force_dm=True,
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

    @bot.command(name="showchannels")
    @restricted_command
    async def showchannels_command(ctx: commands.Context) -> None:
        await _send_channel_configuration(ctx)

    @bot.command(name="setinternchannel")
    @restricted_command
    async def setinternchannel_command(ctx: commands.Context, channel_id: str) -> None:
        try:
            parsed_channel_id = parse_channel_command_value(channel_id)
            channel = await _resolve_configurable_channel(parsed_channel_id)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        await _set_channel_override(
            key=RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY,
            channel_id=parsed_channel_id,
            updated_by_user_id=str(ctx.author.id),
        )
        await ctx.send(
            f"Internship posting channel updated to `{channel.id}` ({channel.guild.name} / #{channel.name})."
        )
        await _send_channel_configuration(ctx)

    @bot.command(name="setnewgradchannel")
    @restricted_command
    async def setnewgradchannel_command(ctx: commands.Context, channel_id: str) -> None:
        try:
            parsed_channel_id = parse_channel_command_value(channel_id, allow_off=True)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        warning_message = ""
        if parsed_channel_id is None:
            await _set_channel_override(
                key=RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
                channel_id=None,
                updated_by_user_id=str(ctx.author.id),
            )
            await ctx.send("New-grad posting has been disabled for runtime routing.")
        else:
            try:
                channel = await _resolve_configurable_channel(parsed_channel_id)
            except ValueError as exc:
                await ctx.send(str(exc))
                return

            await _set_channel_override(
                key=RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
                channel_id=parsed_channel_id,
                updated_by_user_id=str(ctx.author.id),
            )
            if not startup_new_grad_collection_enabled:
                warning_message = (
                    "\nWarning: new-grad collection is currently disabled at startup, "
                    "so no new-grad jobs will post until that is enabled outside Discord."
                )
            await ctx.send(
                f"New-grad posting channel updated to `{channel.id}` ({channel.guild.name} / #{channel.name})."
                f"{warning_message}"
            )

        await _send_channel_configuration(ctx)

    async def _send_helpjobs(ctx: commands.Context) -> None:
        pages = build_help_page_definitions()
        await _send_paginated_embed_pages(
            ctx,
            total_items=len(pages),
            page_size=1,
            build_page=lambda page_index: build_help_embed(page_index),
        )

    @bot.command(name="help")
    async def help_command(ctx: commands.Context) -> None:
        await _send_helpjobs(ctx)
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
        if action in {"help", "dashboard"}:
            if action == "dashboard":
                await _handle_myjobs(ctx, profile, joined_recently=not bool(existing))
                return
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
        if action == "processcompany":
            await _handle_process_company(ctx, profile, payload)
            return
        if action == "processnote":
            await _handle_process_note(ctx, profile, payload)
            return
        if action == "processnotes":
            await _handle_process_notes(ctx, profile, payload)
            return
        if action == "processreport":
            await _handle_process_report(ctx, profile, payload)
            return
        if action == "processdeletenote":
            await _handle_process_delete_note(ctx, profile, payload)
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
