from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import discord

from job_bot.database import Database
from job_bot.models import AppConfig, CanonicalJob


def _truncate(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class SingleChannelPublisher:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.client: discord.Client | None = None
        self.logger = logging.getLogger(self.__class__.__name__)

    def bind_client(self, client: discord.Client) -> None:
        self.client = client

    async def publish_pending(self) -> tuple[int, int]:
        if self.client is None or self.client.user is None:
            return 0, 0
        deliveries = await self.database.list_deliveries_for_send(self.config.discord_channel_id)
        if not deliveries:
            return 0, 0

        channel = await self._resolve_channel()
        self._validate_permissions(channel)
        existing_markers = await self._load_existing_markers(channel, deliveries)
        sent = 0
        reconciled = 0

        for delivery in deliveries:
            canonical_id = int(delivery["canonical_id"])
            marker = str(delivery["marker"])
            jobs = await self.database.get_canonical_jobs([canonical_id])
            if not jobs:
                continue
            job = jobs[0]
            existing_message_id = existing_markers.get(marker)
            if existing_message_id is not None:
                await self.database.mark_delivery_sent(
                    delivery_id=int(delivery["id"]),
                    canonical_id=canonical_id,
                    track=job.track,
                    channel_id=self.config.discord_channel_id,
                    message_id=existing_message_id,
                )
                reconciled += 1
                self.logger.info(
                    "delivery_reconciled delivery=%s canonical=%s message=%s",
                    delivery["id"],
                    canonical_id,
                    existing_message_id,
                )
                continue

            metadata = await self.database.get_feed_observation_metadata(canonical_id)
            await self.database.mark_delivery_sending(int(delivery["id"]))
            try:
                message = await channel.send(
                    embed=self.build_embed(job, marker=marker, metadata=metadata)
                )
            except (discord.HTTPException, asyncio.TimeoutError) as exc:
                await self.database.mark_delivery_uncertain(int(delivery["id"]), str(exc))
                self.logger.warning(
                    "delivery_uncertain delivery=%s canonical=%s error=%s",
                    delivery["id"],
                    canonical_id,
                    exc,
                )
                continue

            await self.database.mark_delivery_sent(
                delivery_id=int(delivery["id"]),
                canonical_id=canonical_id,
                track=job.track,
                channel_id=self.config.discord_channel_id,
                message_id=int(message.id),
            )
            existing_markers[marker] = int(message.id)
            sent += 1
            self.logger.info(
                "delivery_sent delivery=%s canonical=%s message=%s",
                delivery["id"],
                canonical_id,
                message.id,
            )
            if self.config.publish_delay_seconds > 0:
                await asyncio.sleep(self.config.publish_delay_seconds)
        return sent, reconciled

    def build_embed(
        self,
        job: CanonicalJob,
        *,
        marker: str,
        metadata: dict[str, Any],
    ) -> discord.Embed:
        embed = discord.Embed(
            title=_truncate(job.title, 256),
            url=job.canonical_url,
            description=f"[Apply here]({job.canonical_url})",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Company",
            value=_truncate(job.company or "Unknown", 1_024),
            inline=True,
        )
        embed.add_field(
            name="Location",
            value=_truncate(job.location or "United States", 1_024),
            inline=True,
        )
        embed.add_field(
            name="Track",
            value=job.track.replace("_", " ").title(),
            inline=True,
        )
        embed.add_field(
            name="Cycle",
            value=_truncate(job.season_term or "Not stated", 1_024),
            inline=True,
        )
        if metadata.get("posted_date"):
            embed.add_field(name="Posted", value=metadata["posted_date"], inline=True)
        embed.set_footer(text=f"Job Bot • {marker}")
        return embed

    async def _resolve_channel(self) -> Any:
        if self.client is None:
            raise RuntimeError("Discord client is not bound")
        channel = self.client.get_channel(self.config.discord_channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(self.config.discord_channel_id)
        if not hasattr(channel, "send") or not hasattr(channel, "history"):
            raise RuntimeError("DISCORD_CHANNEL_ID must identify a message channel")
        return channel

    def _validate_permissions(self, channel: Any) -> None:
        guild = getattr(channel, "guild", None)
        member = getattr(guild, "me", None) if guild is not None else None
        if member is None:
            raise RuntimeError("Unable to resolve the bot member for channel permission checks")
        permissions = channel.permissions_for(member)
        required = {
            "view_channel": getattr(permissions, "view_channel", False),
            "embed_links": getattr(permissions, "embed_links", False),
            "read_message_history": getattr(permissions, "read_message_history", False),
        }
        channel_type = getattr(channel, "type", None)
        if channel_type in {
            discord.ChannelType.news_thread,
            discord.ChannelType.private_thread,
            discord.ChannelType.public_thread,
        }:
            required["send_messages_in_threads"] = getattr(
                permissions, "send_messages_in_threads", False
            )
        else:
            required["send_messages"] = getattr(permissions, "send_messages", False)
        missing = [name for name, allowed in required.items() if not allowed]
        if missing:
            raise RuntimeError(
                "Discord channel is missing permissions required for duplicate-safe delivery: "
                + ", ".join(missing)
            )

    async def _load_existing_markers(
        self,
        channel: Any,
        deliveries: list[Any],
    ) -> dict[str, int]:
        timestamps = [datetime.fromisoformat(str(row["created_at"])) for row in deliveries]
        after = min(timestamps) - timedelta(minutes=5)
        markers = {str(row["marker"]) for row in deliveries}
        found: dict[str, int] = {}
        try:
            async for message in channel.history(limit=None, after=after, oldest_first=False):
                author = getattr(message, "author", None)
                if self.client is None or author is None or author.id != self.client.user.id:
                    continue
                for embed in getattr(message, "embeds", []):
                    footer = getattr(getattr(embed, "footer", None), "text", None) or ""
                    for marker in markers:
                        if marker in footer:
                            found[marker] = int(message.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise RuntimeError(
                "Unable to reconcile Discord history; refusing to send duplicate-prone messages"
            ) from exc
        return found
