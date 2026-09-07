from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from job_bot.models import AppConfig, CanonicalJob
from outsource.categories import Category, classify_role


class OutsourceMirror:
    """Group successful production jobs into daily cards in per-track channels.

    Mirroring is deliberately best-effort. A destination outage must never turn a
    successful production delivery into an uncertain delivery that can be posted
    to the production channel a second time.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client: discord.Client | None = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = asyncio.Lock()
        self._cards: dict[tuple[int, str], discord.Message] = {}

    def bind_client(self, client: discord.Client) -> None:
        self.client = client

    async def copy_message(
        self,
        *,
        track: str,
        source_channel_id: int,
        source_message_id: int,
        content: str | None,
        embed: discord.Embed,
        job: CanonicalJob | None = None,
    ) -> int | None:
        destination_channel_id = self.config.outsource_channel_id_for_track(track)
        if destination_channel_id is None:
            return None
        if source_channel_id != self.config.channel_id_for_track(track):
            return None
        if destination_channel_id == source_channel_id:
            self.logger.error(
                "outsource_destination_matches_source track=%s channel=%s",
                track,
                source_channel_id,
            )
            return None
        if self.client is None:
            self.logger.error("outsource_client_not_bound track=%s", track)
            return None

        # Digests belong only in the primary channels. The secondary channel's
        # category cards already provide the daily overview.
        if "jobbot:digest:" in (embed.footer.text or ""):
            return None

        try:
            channel = self.client.get_channel(destination_channel_id)
            if channel is None:
                channel = await self.client.fetch_channel(destination_channel_id)
            if channel is None or not hasattr(channel, "history"):
                raise RuntimeError(
                    f"Outsource destination {destination_channel_id} is not a message channel"
                )

            self._validate_permissions(channel)
            category = classify_role(job.title, job.summary) if job else classify_role(embed.title or "")
            async with self._lock:
                thread = await self._category_thread(channel, category, track)
                copied = await thread.send(
                    content=content,
                    embed=discord.Embed.from_dict(embed.to_dict()),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (RuntimeError, discord.HTTPException, asyncio.TimeoutError) as exc:
            self._cards.clear()
            self.logger.error(
                "outsource_copy_failed track=%s source_channel=%s source_message=%s "
                "destination_channel=%s error=%s",
                track,
                source_channel_id,
                source_message_id,
                destination_channel_id,
                exc,
            )
            return None

        self.logger.info(
            "outsource_copy_sent track=%s source_channel=%s source_message=%s "
            "destination_channel=%s destination_message=%s",
            track,
            source_channel_id,
            source_message_id,
            destination_channel_id,
            copied.id,
        )
        return int(copied.id)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _local_now(self) -> datetime:
        try:
            zone = ZoneInfo(self.config.digest_timezone)
        except ZoneInfoNotFoundError:
            self.logger.warning("category_timezone_invalid; using America/New_York")
            zone = ZoneInfo("America/New_York")
        return self._now().astimezone(zone)

    def _category_day(self) -> date:
        """A posting day rolls over at 00:01 in the configured local timezone."""
        return (self._local_now() - timedelta(minutes=1)).date()

    def _prune_cache(self, day: date) -> None:
        self._cards = {
            key: card for key, card in self._cards.items()
            if key[1].rsplit(":", 1)[-1] == day.isoformat()
        }

    @staticmethod
    def _validate_permissions(channel: discord.TextChannel) -> None:
        guild = getattr(channel, "guild", None)
        member = getattr(guild, "me", None)
        if member is None:
            raise RuntimeError("Unable to resolve bot member for category channel")
        permissions = channel.permissions_for(member)
        required = (
            "view_channel", "send_messages", "embed_links", "read_message_history",
            "create_public_threads", "send_messages_in_threads",
        )
        missing = [name for name in required if not getattr(permissions, name, False)]
        if missing:
            raise RuntimeError("Category channel missing permissions: " + ", ".join(missing))

    async def _category_thread(
        self, channel: discord.TextChannel, category: Category, track: str,
    ) -> discord.Thread:
        day = self._category_day()
        self._prune_cache(day)
        marker = self._category_marker(channel.id, track, category.key, day)
        cache_key = (channel.id, marker)
        card = self._cards.get(cache_key)
        if card is None:
            # Older versions prepared cards at 23:59 the previous day. Keep
            # finding those cards on upgrade rather than duplicating their threads.
            midnight = datetime.combine(day, time.min, tzinfo=self._local_now().tzinfo)
            async for message in channel.history(
                limit=None, after=midnight - timedelta(minutes=2), oldest_first=True,
            ):
                if message.author.id != self.client.user.id:
                    continue
                if any((embed.footer.text or "") == marker for embed in message.embeds):
                    card = message
                    break
            if card is None:
                embed = discord.Embed(
                    title=f"{category.label} {day:%m/%d/%Y}",
                    description=f"**Roles that belong here:** {category.roles}",
                    color=discord.Color.blurple(),
                )
                embed.set_footer(text=marker)
                card = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            self._cards[cache_key] = card
        thread = await self._card_thread(card, category, day)
        if thread.locked:
            raise RuntimeError(f"Category thread {thread.id} is locked")
        # Discord automatically unarchives unlocked threads when sending.
        return thread

    @staticmethod
    def _category_marker(channel_id: int, track: str, key: str, day: date) -> str:
        return f"jobbot:category:{channel_id}:{track}:{key}:{day.isoformat()}"

    @staticmethod
    async def _card_thread(card: discord.Message, category: Category, day: date) -> discord.Thread:
        # Fetch also finds archived threads, which are absent from Discord's
        # gateway cache. A message's attached thread always shares its ID.
        try:
            thread = await card.fetch_thread()
        except discord.NotFound:
            thread = await card.create_thread(
                name=f"{category.label} {day:%m/%d/%Y}", auto_archive_duration=1440,
            )
        return thread
