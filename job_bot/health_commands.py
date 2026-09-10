from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands

from job_bot.database import Database
from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.github_feeds import FEEDS
from job_bot.models import AppConfig


def _relative_time(iso_string: str | None) -> str:
    if not iso_string:
        return "never"
    try:
        then = datetime.fromisoformat(iso_string)
    except ValueError:
        return "unknown"
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - then).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}\u2026"


async def _build_overview_embed(
    database: Database,
    config: AppConfig,
) -> discord.Embed:
    baseline_complete = await database.is_initial_feed_baseline_complete()
    delivery_counts = await database.get_delivery_counts()
    stats = await database.get_stats()
    feed_states = await database.get_all_feed_states()

    feed_state_map = {str(row["source_id"]): row for row in feed_states}
    total_feeds = len(FEEDS)
    erroring = sum(
        1
        for feed in FEEDS
        if feed.source_id in feed_state_map
        and feed_state_map[feed.source_id]["last_error"]
    )
    healthy = total_feeds - erroring

    if not baseline_complete:
        color = discord.Color.red()
        status = "Warming up \u2014 initial baseline in progress"
    elif erroring > 0:
        color = discord.Color.orange()
        status = f"Degraded \u2014 {erroring} feed(s) with errors"
    else:
        color = discord.Color.green()
        status = "All systems healthy"

    env_label = config.bot_environment.upper()
    embed = discord.Embed(
        title=f"[{env_label}] Bot Health Overview",
        color=color,
    )
    embed.add_field(name="Status", value=status, inline=False)
    embed.add_field(
        name="Feed Health",
        value=f"{healthy}/{total_feeds} feeds healthy",
        inline=True,
    )
    embed.add_field(
        name="Baseline",
        value="Complete" if baseline_complete else "In progress",
        inline=True,
    )

    pending = delivery_counts.get("pending", 0)
    sending = delivery_counts.get("sending", 0)
    sent = delivery_counts.get("sent", 0)
    suppressed = delivery_counts.get("suppressed", 0)
    uncertain = delivery_counts.get("uncertain", 0)
    queue_lines = (
        f"Pending: **{pending}** | Sending: **{sending}**\n"
        f"Sent: **{sent}** | Suppressed: **{suppressed}**\n"
        f"Uncertain: **{uncertain}**"
    )
    embed.add_field(name="Delivery Queue", value=queue_lines, inline=False)

    embed.add_field(
        name="Database",
        value=(
            f"Canonical jobs: **{stats.get('total_canonical_jobs', 0)}**\n"
            f"Variants found: **{stats.get('total_jobs_found', 0)}**\n"
            f"Posted: **{stats.get('total_posted', 0)}** | "
            f"Deduplicated: **{stats.get('total_deduplicated', 0)}**"
        ),
        inline=False,
    )
    embed.timestamp = datetime.now(UTC)
    embed.set_footer(text=f"Bot environment: {config.bot_environment}")
    return embed


async def _build_feeds_embed(
    database: Database,
    config: AppConfig,
) -> discord.Embed:
    feed_states = await database.get_all_feed_states()
    feed_state_map = {str(row["source_id"]): row for row in feed_states}

    erroring: list[str] = []
    healthy: list[str] = []

    for feed in FEEDS:
        label = feed.status_label or feed.source_id
        state = feed_state_map.get(feed.source_id)
        if state is None:
            erroring.append(f"\u274c **{label}** \u2014 no state recorded")
            continue

        baseline = "\u2705" if state["baseline_complete"] else "\u23f3"
        checked = _relative_time(state["last_checked_at"])
        success = _relative_time(state["last_success_at"])
        error = state["last_error"]

        if error:
            short_error = _truncate(str(error), 80)
            erroring.append(
                f"\u274c **{label}** [{baseline}]\n"
                f"> Checked: {checked} | Last OK: {success}\n"
                f"> Error: `{short_error}`"
            )
        else:
            healthy.append(
                f"\u2705 **{label}** [{baseline}] \u2014 checked {checked}, OK {success}"
            )

    lines = erroring + healthy
    description = "\n".join(lines) if lines else "No feeds configured."
    if len(description) > 4000:
        description = description[:3997] + "\u2026"

    env_label = config.bot_environment.upper()
    has_errors = len(erroring) > 0
    color = discord.Color.orange() if has_errors else discord.Color.green()
    embed = discord.Embed(
        title=f"[{env_label}] Feed Status",
        description=description,
        color=color,
    )
    embed.timestamp = datetime.now(UTC)
    embed.set_footer(text=f"{len(FEEDS)} feeds tracked")
    return embed


async def _build_deliveries_embed(
    database: Database,
    config: AppConfig,
) -> discord.Embed:
    delivery_counts = await database.get_delivery_counts()
    stats = await database.get_stats()

    pending = delivery_counts.get("pending", 0)
    sending = delivery_counts.get("sending", 0)
    sent = delivery_counts.get("sent", 0)
    suppressed = delivery_counts.get("suppressed", 0)
    uncertain = delivery_counts.get("uncertain", 0)

    if uncertain > 0:
        color = discord.Color.orange()
    elif pending > 100:
        color = discord.Color.orange()
    else:
        color = discord.Color.green()

    env_label = config.bot_environment.upper()
    embed = discord.Embed(
        title=f"[{env_label}] Delivery Queue",
        color=color,
    )
    embed.add_field(name="Pending", value=str(pending), inline=True)
    embed.add_field(name="Sending", value=str(sending), inline=True)
    embed.add_field(name="Sent", value=str(sent), inline=True)
    embed.add_field(name="Suppressed", value=str(suppressed), inline=True)
    embed.add_field(name="Uncertain", value=str(uncertain), inline=True)
    embed.add_field(
        name="Total Posted (canonical)",
        value=str(stats.get("total_posted", 0)),
        inline=True,
    )
    if uncertain > 0:
        embed.add_field(
            name="\u26a0\ufe0f Attention",
            value=(
                f"{uncertain} deliveries have uncertain status. "
                "These may have been posted but the bot lost confirmation. "
                "Check the channel history."
            ),
            inline=False,
        )
    embed.timestamp = datetime.now(UTC)
    embed.set_footer(text=f"Bot environment: {config.bot_environment}")
    return embed


def register_health_commands(
    tree: app_commands.CommandTree,
    *,
    database: Database,
    config: AppConfig,
    publisher: TrackChannelPublisher,
) -> None:
    logger = logging.getLogger("health_commands")

    async def require_posting_guild(
        interaction: discord.Interaction,
        guild: discord.Guild,
    ) -> bool:
        try:
            posting_guild_id = await publisher.configured_guild_id()
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
        if guild.id == posting_guild_id:
            return True
        await interaction.response.send_message(
            "This command can only be used from the bot's posting server.",
            ephemeral=True,
        )
        return False

    @tree.command(
        name="health",
        description="Check bot health, feed status, and delivery queue.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(view="Which health view to display.")
    @app_commands.choices(
        view=[
            app_commands.Choice(name="Overview", value="overview"),
            app_commands.Choice(name="Feeds", value="feeds"),
            app_commands.Choice(name="Deliveries", value="deliveries"),
        ]
    )
    async def health(
        interaction: discord.Interaction,
        view: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command must be used in a server.",
                ephemeral=True,
            )
            return
        if not await require_posting_guild(interaction, guild):
            return

        selected = view.value if view is not None else "overview"
        if selected == "feeds":
            embed = await _build_feeds_embed(database, config)
        elif selected == "deliveries":
            embed = await _build_deliveries_embed(database, config)
        else:
            embed = await _build_overview_embed(database, config)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @health.error
    async def health_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "Only server administrators can view bot health."
        else:
            logger.exception("health_command_failed", exc_info=error)
            message = "Health check failed. Check the bot logs for details."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
