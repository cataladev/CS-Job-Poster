from __future__ import annotations

import asyncio
import logging

import discord

from job_bot.models import AppConfig, CanonicalJob
from outsource.categories import CATEGORIES, CATEGORY_ORDER, Category, classify_role


class OutsourceMirror:
    """Group successful primary deliveries into permanent per-category threads.

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
        self._prepared: set[tuple[int, str]] = set()

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
        if destination_channel_id in {self.config.discord_channel_id, self.config.discord_new_grad_channel_id}:
            self.logger.error(
                "outsource_destination_matches_primary track=%s channel=%s",
                track,
                source_channel_id,
            )
            return None
        if self.client is None:
            self.logger.error("outsource_client_not_bound track=%s", track)
            return None

        # Digests belong only in the primary channels. Consolidated channels
        # organize individual listings by category.
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
            async with self._lock:
                self._cards.clear()
                self._prepared.clear()
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

    async def ensure_categories(self) -> None:
        """Create or recover the full permanent set in each consolidated channel."""
        if self.client is None or self.client.user is None:
            return
        for track in ("internship", "new_grad"):
            channel_id = self.config.outsource_channel_id_for_track(track)
            if channel_id is None:
                continue
            if channel_id in {self.config.discord_channel_id, self.config.discord_new_grad_channel_id}:
                self.logger.error("category_destination_matches_primary channel=%s", channel_id)
                continue
            try:
                channel = self.client.get_channel(channel_id)
                if channel is None:
                    channel = await self.client.fetch_channel(channel_id)
                if channel is None or not hasattr(channel, "history"):
                    raise RuntimeError("Category destination is not a message channel")
                self._validate_permissions(channel)
                async with self._lock:
                    await self._ensure_category_set(channel, track)
            except Exception:
                # A secondary destination must not interrupt primary collection
                # or prevent setup of the other track. Later attempts recover.
                async with self._lock:
                    self._cards.clear()
                    self._prepared.clear()
                self.logger.exception("category_setup_failed channel=%s track=%s", channel_id, track)

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
        # Also complete setup if a listing arrives before the startup task, or
        # after interrupted setup, so arrival order cannot determine card order.
        await self._ensure_category_set(channel, track)
        marker = self._category_marker(channel.id, track, category.key)
        thread = await self._card_thread(self._cards[(channel.id, marker)], category)
        if thread.locked:
            raise RuntimeError(f"Category thread {thread.id} is locked")
        # Discord automatically unarchives unlocked threads when sending.
        return thread

    async def _ensure_category_set(self, channel: discord.TextChannel, track: str) -> None:
        if (channel.id, track) in self._prepared:
            return
        markers = {self._category_marker(channel.id, track, key) for key in CATEGORY_ORDER}
        # Permanent cards may be months old. Recover all matching bot-authored
        # cards in one scan without a date cutoff, including archived threads.
        async for message in channel.history(limit=None, oldest_first=True):
            if message.author.id != self.client.user.id:
                continue
            for embed in message.embeds:
                marker = embed.footer.text or ""
                if marker in markers:
                    self._cards.setdefault((channel.id, marker), message)
        for key in CATEGORY_ORDER:
            category = CATEGORIES[key]
            marker = self._category_marker(channel.id, track, key)
            cache_key = (channel.id, marker)
            card = self._cards.get(cache_key)
            if card is None:
                embed = discord.Embed(
                    title=category.label,
                    description=f"**Roles that belong here:** {category.roles}",
                    color=discord.Color.blurple(),
                )
                embed.set_footer(text=marker)
                card = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            self._cards[cache_key] = card
            await self._card_thread(card, category)
        self._prepared.add((channel.id, track))

    @staticmethod
    def _category_marker(channel_id: int, track: str, key: str) -> str:
        return f"jobbot:category:{channel_id}:{track}:{key}"

    @staticmethod
    async def _card_thread(card: discord.Message, category: Category) -> discord.Thread:
        # Fetch also finds archived threads, which are absent from Discord's
        # gateway cache. A message's attached thread always shares its ID.
        try:
            thread = await card.fetch_thread()
        except discord.NotFound:
            thread = await card.create_thread(
                name=category.label, auto_archive_duration=1440,
            )
        return thread
