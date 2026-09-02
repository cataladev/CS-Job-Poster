from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import discord

from job_bot.models import AppConfig


GROUP_LABELS = {
    "core": "Core feeds",
    "company_sources": "Company sources",
}


def _truncate(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class ScrapeStatusReporter:
    """Send one quiet success or pinging failure update per scrape cycle."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client: discord.Client | None = None
        self.logger = logging.getLogger(self.__class__.__name__)

    def bind_client(self, client: discord.Client) -> None:
        self.client = client

    async def report(
        self,
        *,
        feed_group: str,
        elapsed_seconds: float,
        stats: Any | None,
        unexpected_error: Exception | None = None,
    ) -> int | None:
        channel_id = self.config.discord_log_channel_id
        if channel_id is None or self.client is None:
            return None

        errors = self._errors(stats, unexpected_error)
        failed = bool(errors) and feed_group == "company_sources"
        embed = self._build_embed(
            feed_group=feed_group,
            elapsed_seconds=elapsed_seconds,
            stats=stats,
            errors=errors,
            failed=failed,
        )
        content = "@here" if failed else None
        allowed_mentions = (
            discord.AllowedMentions(
                everyone=True,
                users=False,
                roles=False,
                replied_user=False,
            )
            if failed
            else discord.AllowedMentions.none()
        )

        try:
            channel = self.client.get_channel(channel_id)
            if channel is None:
                channel = await self.client.fetch_channel(channel_id)
            if channel is None or not hasattr(channel, "send"):
                raise RuntimeError(f"Log destination {channel_id} is not a message channel")
            message = await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=allowed_mentions,
            )
        except (RuntimeError, discord.HTTPException, asyncio.TimeoutError) as exc:
            self.logger.error(
                "scrape_status_send_failed feed_group=%s channel=%s error=%s",
                feed_group,
                channel_id,
                exc,
            )
            return None

        self.logger.info(
            "scrape_status_sent feed_group=%s failed=%s channel=%s message=%s",
            feed_group,
            failed,
            channel_id,
            message.id,
        )
        return int(message.id)

    @staticmethod
    def _errors(stats: Any | None, unexpected_error: Exception | None) -> list[str]:
        errors: list[str] = []
        if unexpected_error is not None:
            errors.append(f"{type(unexpected_error).__name__}: {unexpected_error}")
        if stats is not None:
            errors.extend(str(error) for error in getattr(stats, "errors", []) if error)
            errors.extend(
                str(error) for error in getattr(stats, "source_errors", []) if error
            )
        return list(dict.fromkeys(errors))

    def _build_embed(
        self,
        *,
        feed_group: str,
        elapsed_seconds: float,
        stats: Any | None,
        errors: list[str],
        failed: bool,
    ) -> discord.Embed:
        group_label = GROUP_LABELS.get(feed_group, feed_group.replace("_", " ").title())
        if failed:
            error_details = "\n".join(f"• {error}" for error in errors)
            description = "The scrape cycle reported an error.\n\n**Error details**\n" + _truncate(
                error_details,
                3_700,
            )
        elif errors:
            error_details = "\n".join(f"• {error}" for error in errors)
            description = (
                "The core-feed scrape reported an error; no alert was sent."
                "\n\n**Error details**\n"
                + _truncate(error_details, 3_650)
            )
        else:
            description = "The scrape cycle completed without errors."

        embed = discord.Embed(
            title=(
                f"🚨 {group_label} scrape failed"
                if failed
                else (
                    f"⚠️ {group_label} scrape completed with a warning"
                    if errors
                    else f"✅ {group_label} scrape successful"
                )
            ),
            description=description,
            color=(
                discord.Color.red()
                if failed
                else discord.Color.orange() if errors else discord.Color.green()
            ),
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Duration", value=f"{elapsed_seconds:.2f}s", inline=True)
        embed.add_field(
            name="Sources checked",
            value=str(
                getattr(
                    stats,
                    "feeds_checked",
                    getattr(stats, "sources_checked", 0),
                )
                if stats is not None
                else 0
            ),
            inline=True,
        )
        embed.add_field(
            name="Observations",
            value=str(getattr(stats, "observations", 0) if stats is not None else 0),
            inline=True,
        )
        embed.add_field(
            name="New jobs",
            value=str(getattr(stats, "canonical_created", 0) if stats is not None else 0),
            inline=True,
        )
        embed.add_field(
            name="Queued",
            value=str(getattr(stats, "deliveries_queued", 0) if stats is not None else 0),
            inline=True,
        )
        embed.add_field(
            name="Posted",
            value=str(getattr(stats, "deliveries_sent", 0) if stats is not None else 0),
            inline=True,
        )
        embed.set_footer(text="Job Bot • Scrape Status")
        return embed
