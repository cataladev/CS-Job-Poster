from __future__ import annotations

import asyncio
import logging

import discord

from job_bot.models import AppConfig


class OutsourceMirror:
    """Copy successful production sends to optional per-track channels.

    Mirroring is deliberately best-effort. A destination outage must never turn a
    successful production delivery into an uncertain delivery that can be posted
    to the production channel a second time.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client: discord.Client | None = None
        self.logger = logging.getLogger(self.__class__.__name__)

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

        try:
            channel = self.client.get_channel(destination_channel_id)
            if channel is None:
                channel = await self.client.fetch_channel(destination_channel_id)
            if channel is None or not hasattr(channel, "send"):
                raise RuntimeError(
                    f"Outsource destination {destination_channel_id} is not a message channel"
                )

            copied = await channel.send(
                content=content,
                embed=discord.Embed.from_dict(embed.to_dict()),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (RuntimeError, discord.HTTPException, asyncio.TimeoutError) as exc:
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
