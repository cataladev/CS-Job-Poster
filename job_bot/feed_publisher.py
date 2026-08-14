from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import discord

from job_bot.database import Database
from job_bot.models import AppConfig, CanonicalJob
from job_bot.utils.feed_normalization import is_us_location


def _truncate(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class TrackChannelPublisher:
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
        sent = 0
        reconciled = 0
        channel_ids = set(await self.database.list_delivery_channel_ids_for_send())
        channel_ids.add(self.config.discord_channel_id)
        if (
            self.config.discord_new_grad_channel_id is not None
            and self.config.discord_new_grad_channel_id != self.config.discord_channel_id
        ):
            channel_ids.add(self.config.discord_new_grad_channel_id)
        for channel_id in sorted(channel_ids):
            try:
                await self._initialize_digest_cursor(channel_id)
                channel_sent, channel_reconciled = await self._publish_channel(channel_id)
                await self._publish_digest_if_due(channel_id)
            except (RuntimeError, discord.HTTPException) as exc:
                self.logger.error(
                    "delivery_channel_blocked channel=%s error=%s",
                    channel_id,
                    exc,
                )
                continue
            sent += channel_sent
            reconciled += channel_reconciled
        return sent, reconciled

    async def _initialize_digest_cursor(self, channel_id: int) -> None:
        """Avoid summarizing historical posts after a deployment or upgrade."""
        if await self.database.get_digest_cursor(channel_id) is not None:
            return
        await self.database.set_digest_cursor(channel_id, datetime.now(UTC).isoformat())

    async def _publish_digest_if_due(self, channel_id: int) -> None:
        cursor = await self.database.get_digest_cursor(channel_id)
        if cursor is None:  # Defensive fallback if initialization was interrupted.
            await self._initialize_digest_cursor(channel_id)
            return
        try:
            started_at = datetime.fromisoformat(cursor)
        except ValueError:
            self.logger.warning("digest_cursor_invalid channel=%s", channel_id)
            await self.database.set_digest_cursor(channel_id, datetime.now(UTC).isoformat())
            return
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        if (now - started_at).total_seconds() < self.config.digest_interval_seconds:
            return

        deliveries = await self.database.list_sent_deliveries_since(
            channel_id=channel_id,
            sent_after=started_at.isoformat(),
        )
        if not deliveries:
            await self.database.set_digest_cursor(channel_id, now.isoformat())
            return

        channel = await self._resolve_channel(channel_id)
        self.validate_channel_permissions(channel)
        track = str(deliveries[0]["track"])
        if any(str(delivery["track"]) != track for delivery in deliveries):
            raise RuntimeError("A job digest channel must contain one track only")

        marker = self._digest_marker(channel_id, started_at)
        existing_message_id = await self._find_existing_digest_marker(
            channel,
            marker=marker,
            after=started_at - timedelta(minutes=5),
        )
        if existing_message_id is not None:
            await self.database.set_digest_cursor(channel_id, now.isoformat())
            self.logger.info(
                "digest_reconciled channel=%s message=%s", channel_id, existing_message_id
            )
            return

        role_id = self.config.alert_role_id_for_track(track)
        if role_id is not None:
            self.validate_role_mention_permissions(channel)

        message = await channel.send(
            content=f"<@&{role_id}>" if role_id is not None else None,
            embed=self.build_digest_embed(
                deliveries,
                channel=channel,
                marker=marker,
                started_at=started_at,
            ),
            allowed_mentions=(
                discord.AllowedMentions(roles=[discord.Object(id=role_id)])
                if role_id is not None
                else discord.AllowedMentions.none()
            ),
        )
        await self.database.set_digest_cursor(channel_id, now.isoformat())
        self.logger.info(
            "digest_sent channel=%s track=%s jobs=%s message=%s",
            channel_id,
            track,
            len(deliveries),
            message.id,
        )

    async def _publish_channel(self, channel_id: int) -> tuple[int, int]:
        deliveries = await self.database.list_deliveries_for_send(channel_id)
        if not deliveries:
            return 0, 0

        channel = await self._resolve_channel(channel_id)
        self.validate_channel_permissions(channel)
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
                    channel_id=channel_id,
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

            location_parts = [
                part.strip() for part in job.location.split("|") if part.strip()
            ]
            if not location_parts or not all(
                is_us_location(
                    location,
                    trusted_us=False,
                    context=job.canonical_url,
                )
                for location in location_parts
            ):
                await self.database.mark_delivery_suppressed(
                    int(delivery["id"]),
                    "non-U.S. location rejected by publisher",
                )
                self.logger.warning(
                    "delivery_suppressed_non_us delivery=%s canonical=%s location=%s",
                    delivery["id"],
                    canonical_id,
                    job.location,
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
                channel_id=channel_id,
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

    def build_digest_embed(
        self,
        deliveries: list[Any],
        *,
        channel: Any,
        marker: str,
        started_at: datetime,
    ) -> discord.Embed:
        track = str(deliveries[0]["track"])
        track_label = track.replace("_", " ").title()
        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        visible_deliveries = deliveries[:20]
        entries = []
        for delivery in visible_deliveries:
            title = _truncate(delivery["title"], 150)
            company = _truncate(delivery["company"], 80)
            location = _truncate(delivery["location"], 80)
            message_id = delivery["message_id"]
            if guild_id is not None and message_id:
                url = f"https://discord.com/channels/{guild_id}/{channel.id}/{message_id}"
            else:
                url = str(delivery["canonical_url"])
            entries.append(f"• [{company} — {title}]({url})\n  {location}")

        hidden_count = len(deliveries) - len(visible_deliveries)
        if hidden_count:
            entries.append(f"• …and {hidden_count} more in this channel.")
        elapsed_hours = max(1, round((datetime.now(UTC) - started_at).total_seconds() / 3600))
        embed = discord.Embed(
            title=f"{track_label} job digest",
            description="\n".join(entries),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Job Bot • {len(deliveries)} new role(s) in the last {elapsed_hours}h • {marker}")
        return embed

    async def _resolve_channel(self, channel_id: int) -> Any:
        if self.client is None:
            raise RuntimeError("Discord client is not bound")
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        if not hasattr(channel, "send") or not hasattr(channel, "history"):
            raise RuntimeError(f"Configured channel {channel_id} is not a message channel")
        return channel

    def validate_channel_permissions(self, channel: Any, member: Any = None) -> None:
        guild = getattr(channel, "guild", None)
        member = member or (getattr(guild, "me", None) if guild is not None else None)
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

    def validate_role_mention_permissions(self, channel: Any, member: Any = None) -> None:
        guild = getattr(channel, "guild", None)
        member = member or (getattr(guild, "me", None) if guild is not None else None)
        if member is None:
            raise RuntimeError("Unable to resolve the bot member for role-mention checks")
        permissions = channel.permissions_for(member)
        if not getattr(permissions, "mention_everyone", False):
            raise RuntimeError(
                "Discord channel is missing Mention Everyone permission required for job-alert role pings"
            )

    @staticmethod
    def _digest_marker(channel_id: int, started_at: datetime) -> str:
        raw = f"digest:{channel_id}:{started_at.isoformat()}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"jobbot:digest:{digest}"

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

    async def _find_existing_digest_marker(
        self,
        channel: Any,
        *,
        marker: str,
        after: datetime,
    ) -> int | None:
        try:
            async for message in channel.history(limit=None, after=after, oldest_first=False):
                author = getattr(message, "author", None)
                if self.client is None or author is None or author.id != self.client.user.id:
                    continue
                for embed in getattr(message, "embeds", []):
                    footer = getattr(getattr(embed, "footer", None), "text", None) or ""
                    if marker in footer:
                        return int(message.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise RuntimeError(
                "Unable to reconcile Discord history; refusing to send duplicate-prone digests"
            ) from exc
        return None
