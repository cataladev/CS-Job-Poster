from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from job_bot.models import AppConfig, CanonicalJob
from outsource.categories import CATEGORIES, CATEGORY_ORDER, Category, classify_role


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
        self._prepared: set[tuple[int, str, date]] = set()

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

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _local_now(self) -> datetime:
        try:
            zone = ZoneInfo(self.config.digest_timezone)
        except ZoneInfoNotFoundError:
            self.logger.warning("category_timezone_invalid; using America/New_York")
            zone = ZoneInfo("America/New_York")
        return self._now().astimezone(zone)

    async def run_category_schedule(self, shutdown: asyncio.Event) -> None:
        """Initialize on startup and prepare tomorrow at 23:59, without feeds."""
        while not shutdown.is_set():
            try:
                await self.ensure_daily_categories()
            except Exception:
                # A secondary-view outage must not stop primary feed polling.
                self.logger.exception("category_schedule_failed")
            now = self._local_now()
            scheduled = now.replace(hour=23, minute=59, second=0, microsecond=0)
            if now >= scheduled:
                scheduled += timedelta(days=1)
            delay = min(60.0, (scheduled.astimezone(UTC) - now.astimezone(UTC)).total_seconds())
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=max(0.1, delay))
            except asyncio.TimeoutError:
                pass

    async def ensure_daily_categories(self) -> None:
        if self.client is None or self.client.user is None:
            return
        now = self._local_now()
        days = [now.date()]
        if now.hour == 23 and now.minute == 59:
            days.append(now.date() + timedelta(days=1))
        async with self._lock:
            self._prune_cache(now.date())
            for track in ("internship", "new_grad"):
                channel_id = self.config.outsource_channel_id_for_track(track)
                if channel_id is None:
                    continue
                if channel_id in {self.config.discord_channel_id, self.config.discord_new_grad_channel_id}:
                    self.logger.error("category_destination_matches_primary channel=%s", channel_id)
                    continue
                for day in days:
                    if (channel_id, track, day) in self._prepared:
                        continue
                    try:
                        channel = self.client.get_channel(channel_id)
                        if channel is None:
                            channel = await self.client.fetch_channel(channel_id)
                        if channel is None or not hasattr(channel, "history"):
                            raise RuntimeError("Category destination is not a message channel")
                        self._validate_permissions(channel)
                        await self._ensure_category_set(channel, track, day)
                    except (RuntimeError, discord.HTTPException, asyncio.TimeoutError):
                        self.logger.exception(
                            "category_setup_failed channel=%s track=%s date=%s", channel_id, track, day,
                        )

    def _prune_cache(self, today: date) -> None:
        keep = {today, today + timedelta(days=1)}
        self._prepared = {key for key in self._prepared if key[2] in keep}
        day_strings = {day.isoformat() for day in keep}
        self._cards = {
            key: card for key, card in self._cards.items()
            if key[1].rsplit(":", 1)[-1] in day_strings
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
        day = self._local_now().date()
        self._prune_cache(day)
        await self._ensure_category_set(channel, track, day)
        marker = self._category_marker(channel.id, track, category.key, day)
        card = self._cards[(channel.id, marker)]
        thread = await self._card_thread(card, category, day)
        if thread.locked:
            raise RuntimeError(f"Category thread {thread.id} is locked")
        # Discord automatically unarchives unlocked threads when sending.
        return thread

    @staticmethod
    def _category_marker(channel_id: int, track: str, key: str, day: date) -> str:
        return f"jobbot:category:{channel_id}:{track}:{key}:{day.isoformat()}"

    async def _ensure_category_set(
        self, channel: discord.TextChannel, track: str, day: date,
    ) -> None:
        if (channel.id, track, day) in self._prepared:
            return
        markers = {
            self._category_marker(channel.id, track, key, day): key for key in CATEGORY_ORDER
        }
        # Tomorrow's set may already have been created at 23:59 the day before.
        # Include that minute when recovering from Discord history on restart.
        midnight = datetime.combine(day, time.min, tzinfo=self._local_now().tzinfo)
        async for message in channel.history(
            limit=None, after=midnight - timedelta(minutes=2), oldest_first=True,
        ):
            if message.author.id != self.client.user.id:
                continue
            for embed in message.embeds:
                marker = embed.footer.text or ""
                if marker in markers:
                    self._cards.setdefault((channel.id, marker), message)

        for marker, key in markers.items():
            category = CATEGORIES[key]
            cache_key = (channel.id, marker)
            card = self._cards.get(cache_key)
            if card is None:
                embed = discord.Embed(
                    title=f"{category.label} {day:%m/%d/%Y}",
                    description=f"**Roles that belong here:** {category.roles}",
                    color=discord.Color.blurple(),
                )
                embed.set_footer(text=marker)
                card = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            self._cards[cache_key] = card
            await self._card_thread(card, category, day)
        self._prepared.add((channel.id, track, day))

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
