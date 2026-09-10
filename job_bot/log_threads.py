"""Permanent Discord destinations for scrape summaries and operational alerts."""

from __future__ import annotations

import asyncio
import logging

import discord

from job_bot.models import AppConfig
from job_bot.utils.notify import WEBHOOK_CONTENT_LIMIT, _mentioned_user_ids


LOG_THREADS = {
    "core": (
        "Core feeds",
        "Core feed scrape results and Jobright health, recovery, and session alerts.",
    ),
    "direct": (
        "Direct scrapers",
        "Scrape results and errors from first-party company sources, including IBM and Microsoft.",
    ),
}


class LogThreads:
    """Recover permanent starter cards before creating any missing threads."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client: discord.Client | None = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = asyncio.Lock()
        self._cards: dict[tuple[int, str], discord.Message] = {}
        self._prepared: set[int] = set()

    def bind_client(self, client: discord.Client) -> None:
        self.client = client

    async def invalidate(self) -> None:
        # Rescan after an uncertain Discord response before creating anything else.
        async with self._lock:
            self._cards.clear()
            self._prepared.clear()

    async def ensure_threads(self) -> None:
        if self.config.discord_log_channel_id is None or self.client is None:
            return
        try:
            async with self._lock:
                await self._prepare()
        except (RuntimeError, discord.HTTPException, asyncio.TimeoutError):
            await self.invalidate()
            self.logger.exception("log_thread_setup_failed channel=%s", self.config.discord_log_channel_id)

    async def thread_for(self, feed_group: str) -> discord.Thread:
        key = "direct" if feed_group in {"company_sources", "direct_ats"} else "core"
        async with self._lock:
            channel = await self._prepare()
            thread = await self._card_thread(self._cards[(channel.id, key)], key)
            if thread.locked:
                raise RuntimeError(f"Log thread {thread.id} is locked")
            # Sending automatically unarchives an unlocked thread on Discord.
            return thread

    async def _prepare(self) -> discord.TextChannel:
        channel_id = self.config.discord_log_channel_id
        if self.client is None or self.client.user is None or channel_id is None:
            raise RuntimeError("Discord log destination is not configured or ready")
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        if channel is None or getattr(channel, "type", None) not in {
            discord.ChannelType.text, discord.ChannelType.news,
        }:
            raise RuntimeError(f"Log destination {channel_id} is not a text channel")
        member = getattr(getattr(channel, "guild", None), "me", None)
        if member is None:
            raise RuntimeError("Unable to resolve bot member for log channel")
        permissions = channel.permissions_for(member)
        required = (
            "view_channel", "send_messages", "embed_links", "read_message_history",
            "create_public_threads", "send_messages_in_threads",
        )
        missing = [name for name in required if not getattr(permissions, name, False)]
        if missing:
            raise RuntimeError("Log channel missing permissions: " + ", ".join(missing))
        if channel.id in self._prepared:
            return channel

        markers = {f"jobbot:log-thread:{channel.id}:{key}": key for key in LOG_THREADS}
        # No date cutoff: threads and their starter cards survive restarts and
        # automatic archiving. Shared dev/prod channels reuse the same bot-authored
        # cards, rather than creating a pair for every environment or bot identity.
        async for message in channel.history(limit=None, oldest_first=False):
            if message.author.id != self.client.user.id and not getattr(message.author, "bot", False):
                continue
            for embed in message.embeds:
                key = markers.get(embed.footer.text or "")
                if key:
                    self._cards.setdefault((channel.id, key), message)
            if all((channel.id, key) in self._cards for key in LOG_THREADS):
                break
        for key, (name, description) in LOG_THREADS.items():
            card = self._cards.get((channel.id, key))
            if card is None:
                embed = discord.Embed(title=name, description=description, color=discord.Color.blurple())
                embed.set_footer(text=f"jobbot:log-thread:{channel.id}:{key}")
                card = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                self._cards[(channel.id, key)] = card
            await self._card_thread(card, key)
        self._prepared.add(channel.id)
        return channel

    @staticmethod
    async def _card_thread(card: discord.Message, key: str) -> discord.Thread:
        try:
            return await card.fetch_thread()
        except discord.NotFound:
            return await card.create_thread(name=LOG_THREADS[key][0], auto_archive_duration=1440)


class CoreLogNotifier:
    """Send Jobright transitions and session alerts through the core log thread."""

    def __init__(self, log_threads: LogThreads) -> None:
        self.log_threads = log_threads
        self.logger = logging.getLogger(self.__class__.__name__)

    async def send(self, message: str) -> bool:
        environment = self.log_threads.config.bot_environment.upper()
        self.logger.info("notification environment=%s %s", environment, message)
        if self.log_threads.config.discord_log_channel_id is None:
            return True
        content = f"[{environment}] {message}"[:WEBHOOK_CONTENT_LIMIT]
        try:
            thread = await self.log_threads.thread_for("core")
            await thread.send(
                content=content,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, replied_user=False,
                    users=[discord.Object(id=int(user_id)) for user_id in _mentioned_user_ids(content)],
                ),
            )
        except (RuntimeError, discord.HTTPException, asyncio.TimeoutError):
            await self.log_threads.invalidate()
            self.logger.exception("core_notification_send_failed environment=%s", environment)
            return False
        return True
